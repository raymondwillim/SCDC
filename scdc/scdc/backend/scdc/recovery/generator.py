"""Candidate generation (Layer 5): Stage 1 (identify problems) to Stage 3 (normalize).

Rule templates only propose actions built from recorded entities and numbers.
They do not decide feasibility: every candidate, including obviously weak
ones worth showing (e.g. a cheaper supplier that is too slow), goes through
``validate_plan``. Templates are skipped when the data rules them out
completely (no spare stock recorded, supplier offers 0 units, commitment not
reschedulable), so the owner sees a short, honest list.

Templates, in output order:
  1. use_spare_inventory        owned-but-not-on-hand units cover the shortfall
  2. source_alternative_supplier one plan per recorded offer from another supplier
  3. combination                spare + offers that each arrive in time but are
                                individually too small (only if no single source fits)
  3b. partial + expedite        in-time stock is not enough even combined: take all of
                                it and ask the disrupted supplier to rush only the rest
  4. reschedule_job             move one affected job whose commitments are all
                                reschedulable to the first later day that has room
  5. contact_supplier           ask the disrupted supplier to expedite the delayed units

reallocate_resource and split_allocation are not emitted by the rules: the
records hold no minimum acceptable quantity or partial-fulfilment permission,
so any such plan would rest on an invented fact. They are still accepted from
AI suggestions and user edits and validated like everything else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .contracts import (
    MATERIAL_INCIDENT_FIELDS,
    ActionType,
    BusinessSnapshot,
    EntityType,
    ImpactResult,
    ImpactStatus,
    NormalizedIncident,
    PlanOrigin,
    RecoveryAction,
    RecoveryPlan,
    TargetEntity,
)
from .costing import estimate_cost
from .state import BusinessState

RESCHEDULE_HORIZON_DAYS = 7


class RecoveryInputError(ValueError):
    """The inputs cannot be planned on (e.g. incident still needs clarification)."""


@dataclass
class IdFactory:
    """Sequential contract IDs. P2 passes the next free numbers from the DB."""

    next_plan: int = 1
    next_action: int = 1

    def plan_id(self) -> str:
        value = f"RP-{self.next_plan:03d}"
        self.next_plan += 1
        return value

    def action_id(self) -> str:
        value = f"RA-{self.next_action:03d}"
        self.next_action += 1
        return value


@dataclass
class RecoveryProblem:
    """Stage 1 output: one resource that is short, and what is at stake."""

    resource_id: str
    dates: list[date]
    shortfall: int  # largest shortfall over those dates
    need_by: date  # earliest shortfall date; supply must arrive before it
    job_ids: list[str]
    commitment_ids: list[str]


@dataclass
class Candidate:
    """A plan before IDs are assigned."""

    summary: str
    actions: list[tuple[ActionType, TargetEntity, dict[str, Any]]]
    assumptions: list[str] = field(default_factory=list)
    created_by: PlanOrigin = PlanOrigin.rules

    def signature(self) -> str:
        return json.dumps(
            sorted([a.value, json.dumps(p, sort_keys=True, default=str)] for a, _, p in self.actions)
        )


@dataclass
class GenerationResult:
    problems: list[RecoveryProblem]
    plans: list[RecoveryPlan]
    warnings: list[str] = field(default_factory=list)


# ============================================================================ stage 1


def check_incident(incident: NormalizedIncident) -> None:
    unresolved = {u.field for u in incident.unresolved_fields} & set(MATERIAL_INCIDENT_FIELDS)
    missing = {f for f in MATERIAL_INCIDENT_FIELDS if getattr(incident, f) is None}
    if unresolved or missing:
        raise RecoveryInputError(
            f"Incident {incident.id} needs clarification before planning: "
            f"{', '.join(sorted(unresolved | missing))}."
        )


def identify_problems(
    incident: NormalizedIncident, snapshot: BusinessSnapshot, impact: ImpactResult
) -> list[RecoveryProblem]:
    state = BusinessState.from_snapshot(snapshot, incident)
    affected = {
        e.entity_id
        for e in impact.affected_entities
        if e.impact_status in (ImpactStatus.confirmed, ImpactStatus.potential_risk)
    }
    by_resource: dict[str, list] = {}
    for s in impact.resource_shortfalls:
        if s.shortfall > 0:
            by_resource.setdefault(s.resource_id, []).append(s)

    problems = []
    for resource_id, shortfalls in sorted(by_resource.items()):
        dates = sorted(s.date for s in shortfalls)
        jobs = sorted({d.job_id for d in state.demands_for(resource_id) if d.date in dates})
        # Prefer the impact engine's view of which jobs are affected; fall back
        # to the snapshot if it did not list them.
        jobs = [j for j in jobs if j in affected] or jobs
        commitments = sorted(c.id for j in jobs for c in state.commitments_for(j))
        problems.append(
            RecoveryProblem(
                resource_id=resource_id,
                dates=dates,
                shortfall=max(s.shortfall for s in shortfalls),
                need_by=dates[0],
                job_ids=jobs,
                commitment_ids=commitments,
            )
        )
    return problems


# ============================================================================ stage 2: templates


def _fmt(day: date) -> str:
    return f"{day.day} {day:%b}"


def _plural(unit: str, n: int) -> str:
    return unit if n == 1 else f"{unit}s"


def _spare(state: BusinessState, pr: RecoveryProblem) -> list[Candidate]:
    spare = state.spare_quantity(pr.resource_id)
    if spare < pr.shortfall:
        return []
    r = state.resource(pr.resource_id)
    return [
        Candidate(
            summary=f"Use {pr.shortfall} spare {_plural(r.unit, pr.shortfall)} recorded as owned but not on hand.",
            actions=[
                (
                    ActionType.use_spare_inventory,
                    TargetEntity(entity_type=EntityType.resource, entity_id=r.id),
                    {"resource_id": r.id, "quantity": pr.shortfall},
                )
            ],
            assumptions=[f"{pr.shortfall} spare {r.name} can be located and are usable."],
        )
    ]


def _alternative_offers(state: BusinessState, incident: NormalizedIncident, pr: RecoveryProblem):
    return [
        o
        for o in sorted(state.offerings(resource_id=pr.resource_id), key=lambda o: o.id)
        if o.supplier_id != incident.supplier_id  # the disrupted supplier is handled by template 5
        and o.available_quantity != 0
        and state.supplier(o.supplier_id) is not None
    ]


def _source_action(state: BusinessState, offer, qty: int):
    order = state.today
    return (
        ActionType.source_alternative_supplier,
        TargetEntity(entity_type=EntityType.resource, entity_id=offer.resource_id),
        {
            "supplier_id": offer.supplier_id,
            "resource_id": offer.resource_id,
            "quantity": qty,
            "order_date": order.isoformat(),
            "expected_arrival_date": (order + timedelta(days=offer.lead_time_days)).isoformat(),
        },
    )


def _alternatives(state: BusinessState, incident: NormalizedIncident, pr: RecoveryProblem) -> list[Candidate]:
    r = state.resource(pr.resource_id)
    out = []
    for offer in _alternative_offers(state, incident, pr):
        supplier = state.supplier(offer.supplier_id)
        arrival = state.today + timedelta(days=offer.lead_time_days)
        verb = "Rent" if r.reusable else "Buy"
        out.append(
            Candidate(
                summary=f"{verb} {pr.shortfall} {_plural(r.unit, pr.shortfall)} from {supplier.name} "
                f"for delivery on {_fmt(arrival)}.",
                actions=[_source_action(state, offer, pr.shortfall)],
            )
        )
    return out


def _in_time_sources(state: BusinessState, incident: NormalizedIncident, pr: RecoveryProblem):
    """Spare stock + confirmed offers that arrive before the need date, cheapest first."""
    in_time = [
        o
        for o in _alternative_offers(state, incident, pr)
        if o.availability_confirmed
        and o.available_quantity is not None
        and state.today + timedelta(days=o.lead_time_days) < pr.need_by
    ]
    spare = state.spare_quantity(pr.resource_id)
    sources: list[tuple[str, Any, int]] = []
    if spare:
        sources.append(("spare", None, spare))
    cheapest = sorted(in_time, key=lambda o: (o.unit_cost is None, o.unit_cost.amount if o.unit_cost else 0, o.id))
    sources += [("offer", o, o.available_quantity) for o in cheapest]
    return sources


def _single_source_covers(sources, shortfall: int) -> bool:
    return any(qty >= shortfall for _, _, qty in sources)


def _take(state: BusinessState, pr: RecoveryProblem, sources, needed: int):
    """Greedily take up to ``needed`` units. Returns (actions, assumptions, parts, taken)."""
    r = state.resource(pr.resource_id)
    remaining, actions, assumptions, parts = needed, [], [], []
    for kind, offer, qty in sources:
        if remaining <= 0:
            break
        take = min(qty, remaining)
        remaining -= take
        if kind == "spare":
            actions.append(
                (
                    ActionType.use_spare_inventory,
                    TargetEntity(entity_type=EntityType.resource, entity_id=r.id),
                    {"resource_id": r.id, "quantity": take},
                )
            )
            assumptions.append(f"{take} spare {r.name} can be located and are usable.")
            parts.append(f"{take} spare")
        else:
            actions.append(_source_action(state, offer, take))
            parts.append(f"{take} from {state.supplier(offer.supplier_id).name}")
    return actions, assumptions, parts, needed - remaining


def _combination(state: BusinessState, incident: NormalizedIncident, pr: RecoveryProblem) -> list[Candidate]:
    sources = _in_time_sources(state, incident, pr)
    if _single_source_covers(sources, pr.shortfall):
        return []  # a single source already covers it
    actions, assumptions, parts, taken = _take(state, pr, sources, pr.shortfall)
    if taken < pr.shortfall or len(actions) < 2:
        return []
    r = state.resource(pr.resource_id)
    return [
        Candidate(
            summary=f"Combine sources for {pr.shortfall} {_plural(r.unit, pr.shortfall)}: {', '.join(parts)}.",
            actions=actions,
            assumptions=assumptions,
        )
    ]


def _partial_plus_expedite(
    state: BusinessState, incident: NormalizedIncident, pr: RecoveryProblem
) -> list[Candidate]:
    """In-time stock cannot cover the gap: take all of it and ask the late supplier to rush only the rest."""
    delayed = state.delayed_arrival
    if delayed is None or delayed.resource_id != pr.resource_id or delayed.date < pr.need_by:
        return []
    requested = pr.need_by - timedelta(days=1)
    if requested < state.today:
        return []
    sources = _in_time_sources(state, incident, pr)
    if _single_source_covers(sources, pr.shortfall):
        return []
    actions, assumptions, parts, taken = _take(state, pr, sources, pr.shortfall)
    rest = pr.shortfall - taken
    if taken == 0 or rest <= 0 or rest > delayed.quantity:
        return []  # nothing to combine, already covered, or asking for more than is late

    supplier = state.supplier(incident.supplier_id)
    r = state.resource(pr.resource_id)
    unit = _plural(r.unit, rest)
    actions.append(
        (
            ActionType.contact_supplier,
            TargetEntity(entity_type=EntityType.supplier, entity_id=supplier.id),
            {
                "supplier_id": supplier.id,
                "request": f"Deliver {rest} of the remaining {delayed.quantity} "
                f"{_plural(r.unit, delayed.quantity)} by {_fmt(requested)}",
                "requested_date": requested.isoformat(),
                "resource_id": r.id,
                "quantity": rest,
            },
        )
    )
    assumptions.append(
        f"{supplier.id} can deliver {rest} of the remaining {delayed.quantity} "
        f"{_plural(r.unit, delayed.quantity)} by {requested.isoformat()}."
    )
    return [
        Candidate(
            summary=f"Get {', '.join(parts)}, and ask {supplier.name} to deliver {rest} {unit} by {_fmt(requested)}.",
            actions=actions,
            assumptions=assumptions,
        )
    ]


def _reschedule(state: BusinessState, pr: RecoveryProblem) -> list[Candidate]:
    out = []
    for job_id in pr.job_ids:
        commitments = state.commitments_for(job_id)
        if not commitments or not all(c.reschedulable for c in commitments):
            continue
        demand = next((d for d in state.demands_for(pr.resource_id, job_id) if d.date in pr.dates), None)
        if demand is None or demand.quantity < pr.shortfall:
            continue  # moving this job alone would not close the gap
        for offset in range(1, RESCHEDULE_HORIZON_DAYS + 1):
            day = demand.date + timedelta(days=offset)
            if state.pool(pr.resource_id, day) - state.demand_on(pr.resource_id, day) >= demand.quantity:
                job = state.job(job_id)
                out.append(
                    Candidate(
                        summary=f"Move {job.name} from {_fmt(demand.date)} to {_fmt(day)}.",
                        actions=[
                            (
                                ActionType.reschedule_job,
                                TargetEntity(entity_type=EntityType.job, entity_id=job_id),
                                {"job_id": job_id, "new_date": day.isoformat()},
                            )
                        ],
                    )
                )
                break
    return out


def _expedite(state: BusinessState, incident: NormalizedIncident, pr: RecoveryProblem) -> list[Candidate]:
    delayed = state.delayed_arrival
    if delayed is None or delayed.resource_id != pr.resource_id or delayed.date < pr.need_by:
        return []
    requested = pr.need_by - timedelta(days=1)
    if requested < state.today:
        return []
    supplier = state.supplier(incident.supplier_id)
    r = state.resource(pr.resource_id)
    unit = _plural(r.unit, delayed.quantity)
    return [
        Candidate(
            summary=f"Ask {supplier.name} to deliver the remaining {delayed.quantity} {unit} by {_fmt(requested)}.",
            actions=[
                (
                    ActionType.contact_supplier,
                    TargetEntity(entity_type=EntityType.supplier, entity_id=supplier.id),
                    {
                        "supplier_id": supplier.id,
                        "request": f"Deliver remaining {delayed.quantity} {unit} by {_fmt(requested)}",
                        "requested_date": requested.isoformat(),
                    },
                )
            ],
            assumptions=[
                f"{supplier.id} can expedite the remaining {delayed.quantity} {unit} to {requested.isoformat()}."
            ],
        )
    ]


# ============================================================================ stage 3: normalize


def to_plan(candidate: Candidate, disruption_id: str, ids: IdFactory, snapshot: BusinessSnapshot) -> RecoveryPlan:
    plan = RecoveryPlan(
        id=ids.plan_id(),
        disruption_id=disruption_id,
        summary=candidate.summary,
        actions=[
            RecoveryAction(id=ids.action_id(), action_type=t, target_entity=target, parameters=params)
            for t, target, params in candidate.actions
        ],
        assumptions=candidate.assumptions,
        created_by=candidate.created_by,
    )
    plan.estimated_cost = estimate_cost(plan, snapshot)
    return plan


def generate_candidates(
    incident: NormalizedIncident,
    snapshot: BusinessSnapshot,
    impact: ImpactResult,
    *,
    ids: IdFactory | None = None,
    suggester=None,
) -> GenerationResult:
    """Stages 1–3. Validation is *not* filled in here."""
    from .ai_adapter import NullSuggester, collect_suggestions  # local import avoids a cycle

    check_incident(incident)
    ids = ids or IdFactory()
    state = BusinessState.from_snapshot(snapshot, incident)
    problems = identify_problems(incident, snapshot, impact)

    candidates: list[Candidate] = []
    for pr in problems:
        candidates += _spare(state, pr)
        candidates += _alternatives(state, incident, pr)
        candidates += _combination(state, incident, pr)
        candidates += _partial_plus_expedite(state, incident, pr)
        candidates += _reschedule(state, pr)
        candidates += _expedite(state, incident, pr)

    ai_candidates, warnings = collect_suggestions(suggester or NullSuggester(), incident, snapshot, impact, problems)
    candidates += ai_candidates

    seen: set[str] = set()
    plans = []
    for c in candidates:
        sig = c.signature()
        if sig in seen:
            if c.created_by == PlanOrigin.ai_suggestion:
                warnings.append(f"AI suggestion duplicated an existing plan and was dropped: {c.summary}")
            continue
        seen.add(sig)
        plans.append(to_plan(c, incident.id, ids, snapshot))
    return GenerationResult(problems=problems, plans=plans, warnings=warnings)


def generate_plans(
    incident: NormalizedIncident,
    snapshot: BusinessSnapshot,
    impact: ImpactResult,
    *,
    ids: IdFactory | None = None,
    suggester=None,
) -> list[RecoveryPlan]:
    """Contract §5 entry point."""
    return generate_candidates(incident, snapshot, impact, ids=ids, suggester=suggester).plans
