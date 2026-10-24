"""Convenience entry points that chain the stages, plus the edit flow.

    plan_recovery()  generate -> validate each -> rank -> explain
    edit_plan()      new version from an owner's edit, always revalidated
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .contracts import (
    BusinessSnapshot,
    ImpactResult,
    NormalizedIncident,
    PlanOrigin,
    PlanStatus,
    RecoveryAction,
    RecoveryPlan,
    ValidationStatus,
)
from .costing import estimate_cost
from .explain import PlanExplanation, explain_plan
from .generator import IdFactory, RecoveryProblem, generate_candidates
from .ranking import PlanScore, RankingWeights, rank_with_report
from .validator import validate_plan


class StalePlanVersion(ValueError):
    """Maps to HTTP 409 STALE_PLAN_VERSION."""


@dataclass
class RecoveryResult:
    problems: list[RecoveryProblem]
    plans: list[RecoveryPlan]  # ranked order
    scores: list[PlanScore]
    explanations: list[PlanExplanation]
    warnings: list[str] = field(default_factory=list)

    @property
    def recommended(self) -> RecoveryPlan | None:
        return next((p for p in self.plans if p.rank == 1), None)

    @property
    def no_feasible_plan(self) -> bool:
        return self.recommended is None

    def next_steps(self) -> list[str]:
        if not self.no_feasible_plan:
            return [f"Review {self.recommended.id} and approve, edit or reject it."]
        pending = [p for p in self.plans if p.validation.status == ValidationStatus.needs_confirmation]
        if pending:
            return [f"No plan is validated yet. Confirm: {c}" for p in pending for c in p.validation.unresolved_assumptions]
        return ["No recorded option recovers the shortfall. Contact suppliers/customers outside the system."]


def _commitment_jobs(snapshot: BusinessSnapshot) -> dict[str, str]:
    return {c.id: c.job_id for c in snapshot.commitments}


def _requirements(snapshot: BusinessSnapshot) -> dict[tuple[str, str], int]:
    return {
        (d.target_id, d.source_id): d.required_quantity
        for d in snapshot.dependencies
        if d.dependency_type.value == "requires" and d.required_quantity is not None
    }


def revalidate(
    plan: RecoveryPlan,
    snapshot: BusinessSnapshot,
    impact: ImpactResult,
    incident: NormalizedIncident,
    validated_at: datetime | None = None,
) -> RecoveryPlan:
    out = plan.model_copy(deep=True)
    out.validation = validate_plan(out, snapshot, impact, incident, validated_at=validated_at)
    if out.validation.status != ValidationStatus.feasible:
        out.rank = None
    return out


def plan_recovery(
    incident: NormalizedIncident,
    snapshot: BusinessSnapshot,
    impact: ImpactResult,
    *,
    ids: IdFactory | None = None,
    suggester=None,
    weights: RankingWeights = RankingWeights(),
    validated_at: datetime | None = None,
) -> RecoveryResult:
    gen = generate_candidates(incident, snapshot, impact, ids=ids, suggester=suggester)
    validated = [revalidate(p, snapshot, impact, incident, validated_at) for p in gen.plans]
    ranked, scores = rank_with_report(validated, impact, _commitment_jobs(snapshot), weights, _requirements(snapshot))
    by_id = {s.plan_id: s for s in scores}
    explanations = [explain_plan(p, snapshot, by_id[p.id]) for p in ranked]
    return RecoveryResult(gen.problems, ranked, scores, explanations, gen.warnings)


def edit_plan(
    plan: RecoveryPlan,
    *,
    base_version: int,
    actions: list[dict[str, Any] | RecoveryAction],
    assumptions: list[str] | None,
    snapshot: BusinessSnapshot,
    impact: ImpactResult,
    incident: NormalizedIncident,
    validated_at: datetime | None = None,
) -> tuple[RecoveryPlan, RecoveryPlan]:
    """Apply an owner's edit. Returns ``(superseded_old, new_version)``.

    The new version is always revalidated and re-costed: an edit can turn a
    feasible plan infeasible, and approval must look at the new result.
    Rank is cleared; call ``rank_plans`` on the current set to re-rank.
    """
    if base_version != plan.version:
        raise StalePlanVersion(f"{plan.id} is at version {plan.version}, edit was based on {base_version}.")
    new = RecoveryPlan(
        id=plan.id,
        disruption_id=plan.disruption_id,
        version=plan.version + 1,
        summary=plan.summary,
        actions=[a if isinstance(a, RecoveryAction) else RecoveryAction.model_validate(a) for a in actions],
        assumptions=list(plan.assumptions if assumptions is None else assumptions),
        created_by=PlanOrigin.user_edit,
    )
    new.summary = f"{plan.summary} (edited, v{new.version})"
    new.estimated_cost = estimate_cost(new, snapshot)
    new = revalidate(new, snapshot, impact, incident, validated_at)
    old = plan.model_copy(update={"status": PlanStatus.superseded})
    return old, new
