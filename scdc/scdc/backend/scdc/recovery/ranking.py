"""Plan ranking (Layer 7). Documented, configurable, deterministic.

Only ``feasible`` plans get a rank (1 = best). ``needs_confirmation`` plans get
a *provisional* score so the UI can order them, but ``rank`` stays null until
the missing facts are confirmed and the plan is revalidated. ``infeasible``
plans are never ranked.

Score = sum(weight_i * factor_i) / sum(weight_i), every factor in [0, 1]:

  protection   affected commitments still fully met on their original date
               / affected commitments                       (from the impact result)
  deadlines    affected jobs kept on their original date with their full
               quantity / affected jobs  (moved or reduced jobs do not count)
  cost         1 for the cheapest known cost among compared plans, 0 for the
               most expensive, linear in between; 0 when cost is unknown
               (unknown cost is never treated as cheap)
  simplicity   1 / number of actions (fewer operational changes)
  certainty    mean of (1 - min(1, unresolved assumptions / 3)) and
               (1 - share of the shortfall that rests on unconfirmed units:
               supplier rush requests and unverified spare stock), so
               "rent 25 + ask for 15" beats "ask for all 40"

Ties: lower known cost, then fewer actions, then plan id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .contracts import (
    ActionType,
    EntityType,
    ImpactResult,
    ImpactStatus,
    RecoveryPlan,
    ValidationStatus,
)


@dataclass(frozen=True)
class RankingWeights:
    protection: float = 0.35
    deadlines: float = 0.25
    cost: float = 0.20
    certainty: float = 0.15
    simplicity: float = 0.05

    def as_dict(self) -> dict[str, float]:
        return dict(self.__dict__)


@dataclass
class PlanScore:
    plan_id: str
    status: ValidationStatus
    score: float | None  # None for infeasible
    provisional: bool
    factors: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    rank: int | None = None
    reason: str = ""


def _affected(impact: ImpactResult | None, entity_type: EntityType) -> set[str]:
    if impact is None:
        return set()
    return {
        e.entity_id
        for e in impact.affected_entities
        if e.entity_type == entity_type and e.impact_status != ImpactStatus.unresolved
    }


def _moved_jobs(plan: RecoveryPlan) -> set[str]:
    return {
        a.parameters.get("job_id")
        for a in plan.actions
        if a.action_type == ActionType.reschedule_job and isinstance(a.parameters.get("job_id"), str)
    }


def _reduced_jobs(plan: RecoveryPlan, requirements: dict[tuple[str, str], int]) -> set[str]:
    """Jobs a plan leaves with less than their recorded requirement."""
    out: set[str] = set()
    for a in plan.actions:
        p = a.parameters
        if a.action_type == ActionType.split_allocation:
            for alloc in p.get("allocations", []) or []:
                need = requirements.get((alloc.get("job_id"), p.get("resource_id")))
                if need is not None and isinstance(alloc.get("quantity"), int) and alloc["quantity"] < need:
                    out.add(alloc["job_id"])
        elif a.action_type == ActionType.reallocate_resource and isinstance(p.get("from_job_id"), str):
            out.add(p["from_job_id"])
    return out


def _unconfirmed_units(plan: RecoveryPlan, shortfall: int) -> int:
    units = 0
    for a in plan.actions:
        q = a.parameters.get("quantity")
        if a.action_type == ActionType.contact_supplier:
            units += q if isinstance(q, int) else shortfall  # a full rush request when unspecified
        elif a.action_type == ActionType.use_spare_inventory and isinstance(q, int):
            units += q
    return units


def score_plans(
    plans: list[RecoveryPlan],
    impact: ImpactResult | None = None,
    commitment_jobs: dict[str, str] | None = None,
    weights: RankingWeights = RankingWeights(),
    requirements: dict[tuple[str, str], int] | None = None,
) -> list[PlanScore]:
    """``commitment_jobs`` maps commitment id -> job id and ``requirements``
    maps (job id, resource id) -> required quantity, both from the snapshot."""
    jobs = _affected(impact, EntityType.job)
    commitments = _affected(impact, EntityType.commitment)
    commitment_jobs = commitment_jobs or {}
    requirements = requirements or {}
    shortfall = max((s.shortfall for s in impact.resource_shortfalls), default=0) if impact else 0

    known_costs = [
        p.estimated_cost.amount
        for p in plans
        if p.estimated_cost is not None and p.validation and p.validation.status != ValidationStatus.infeasible
    ]
    lo, hi = (min(known_costs), max(known_costs)) if known_costs else (Decimal(0), Decimal(0))
    w = weights.as_dict()
    total_w = sum(w.values()) or 1.0

    scores = []
    for p in plans:
        if p.validation is None:
            raise ValueError(f"{p.id} has not been validated; run validate_plan first.")
        status = p.validation.status
        if status == ValidationStatus.infeasible:
            scores.append(PlanScore(p.id, status, None, False))
            continue

        moved = _moved_jobs(p) | _reduced_jobs(p, requirements)
        notes = []
        f: dict[str, float] = {}
        f["protection"] = (
            sum(1 for c in commitments if commitment_jobs.get(c) not in moved) / len(commitments)
            if commitments
            else 1.0
        )
        f["deadlines"] = sum(1 for j in jobs if j not in moved) / len(jobs) if jobs else 1.0
        if p.estimated_cost is None:
            f["cost"] = 0.0
            notes.append("cost unknown, scored as 0 for cost")
        elif hi == lo:
            f["cost"] = 1.0
        else:
            f["cost"] = float((hi - p.estimated_cost.amount) / (hi - lo))
        f["simplicity"] = 1.0 / len(p.actions)
        open_facts = 1.0 - min(1.0, len(p.validation.unresolved_assumptions) / 3)
        if shortfall:
            relied = min(1.0, _unconfirmed_units(p, shortfall) / shortfall)
            f["certainty"] = (open_facts + (1.0 - relied)) / 2
        else:
            f["certainty"] = open_facts
        score = round(sum(w[k] * f[k] for k in w) / total_w, 4)
        scores.append(
            PlanScore(p.id, status, score, status == ValidationStatus.needs_confirmation, f, notes)
        )
    return scores


def rank_plans(
    plans: list[RecoveryPlan],
    impact: ImpactResult | None = None,
    commitment_jobs: dict[str, str] | None = None,
    weights: RankingWeights = RankingWeights(),
    requirements: dict[tuple[str, str], int] | None = None,
) -> list[RecoveryPlan]:
    """Contract §5 entry point. Returns copies ordered feasible > needs_confirmation > infeasible."""
    ranked, _ = rank_with_report(plans, impact, commitment_jobs, weights, requirements)
    return ranked


def rank_with_report(
    plans: list[RecoveryPlan],
    impact: ImpactResult | None = None,
    commitment_jobs: dict[str, str] | None = None,
    weights: RankingWeights = RankingWeights(),
    requirements: dict[tuple[str, str], int] | None = None,
) -> tuple[list[RecoveryPlan], list[PlanScore]]:
    scores = {s.plan_id: s for s in score_plans(plans, impact, commitment_jobs, weights, requirements)}
    order = {ValidationStatus.feasible: 0, ValidationStatus.needs_confirmation: 1, ValidationStatus.infeasible: 2}

    def key(p: RecoveryPlan):
        s = scores[p.id]
        cost = p.estimated_cost.amount if p.estimated_cost is not None else Decimal("Infinity")
        return (order[s.status], -(s.score or 0.0), cost, len(p.actions), p.id)

    ranked: list[RecoveryPlan] = []
    feasible_rank = 0
    for p in sorted(plans, key=key):
        s = scores[p.id]
        copy = p.model_copy(deep=True)
        if s.status == ValidationStatus.feasible:
            feasible_rank += 1
            copy.rank = s.rank = feasible_rank
        else:
            copy.rank = None
        s.reason = _reason(s, weights)
        ranked.append(copy)
    return ranked, [scores[p.id] for p in ranked]


def _reason(s: PlanScore, weights: RankingWeights) -> str:
    if s.status == ValidationStatus.infeasible:
        return "Not ranked: at least one hard constraint fails."
    parts = ", ".join(f"{k} {v:.2f}x{getattr(weights, k):.2f}" for k, v in s.factors.items())
    head = f"Rank {s.rank}" if s.rank else "Not ranked until confirmed (provisional)"
    extra = f"; {'; '.join(s.notes)}" if s.notes else ""
    return f"{head}: score {s.score:.2f} = {parts}{extra}."
