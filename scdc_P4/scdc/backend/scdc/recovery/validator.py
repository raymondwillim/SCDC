"""Deterministic feasibility validation (Layer 6). No AI, no I/O, no clock.

``validate_plan`` copies the business state, applies every action of the plan
to the copy, then checks the result. Status follows CONTRACTS.md §3:

    any check ``fail``        -> infeasible
    else any ``unresolved``   -> needs_confirmation
    else                      -> feasible

Why each check exists:

RESOURCE_AVAILABLE       The resource / job / stock the plan uses must exist in
                         the records. Prevents plans built on invented stock.
SUPPLIER_AVAILABLE       A supplier "in the database" is not a supplier who can
                         deliver. Only confirmed availability with enough
                         quantity passes; unconfirmed is ``unresolved``.
LEAD_TIME_OK             Order date + recorded lead time must arrive before the
                         need date, and the plan may not claim a faster arrival.
QUANTITY_SUFFICIENT      After the plan, enough units exist for every shortfall
                         day (timing ignored, so "too few" and "too late" are
                         reported separately).
DEADLINE_MET             Those units actually arrive before the day they are
                         needed (an arrival on the event day itself is too late).
NO_DOUBLE_BOOKING        Fixing one day must not break another: other jobs on
                         the same resource keep their coverage, and the same
                         offer / spare stock is not promised twice.
DEPENDENCY_ORDER_OK      Dates are in a possible order: nothing ordered or
                         rescheduled into the past, nothing arrives before it is
                         ordered.
COMMITMENT_RESCHEDULABLE A job may only move if every commitment it fulfils is
                         recorded as reschedulable.
FACTS_KNOWN              Summarises everything the plan relies on that is not
                         confirmed (assumptions, unverified stock, stale data,
                         unresolved impact facts).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .actions import (
    ContactSupplier,
    ParameterError,
    ReallocateResource,
    RescheduleJob,
    SourceAlternativeSupplier,
    SplitAllocation,
    UseSpareInventory,
    parse_parameters,
)
from .contracts import (
    ActionType,
    BusinessSnapshot,
    CheckCode,
    CheckResult,
    ImpactResult,
    NormalizedIncident,
    RecoveryAction,
    RecoveryPlan,
    ValidationCheck,
    ValidationResult,
    ValidationStatus,
)
from .state import Arrival, BusinessState

PASS, FAIL, UNRESOLVED = CheckResult.pass_, CheckResult.fail, CheckResult.unresolved


@dataclass
class _Run:
    """Mutable working data for one validation run."""

    state: BusinessState
    baseline: BusinessState
    incident: NormalizedIncident
    targets: dict[str, list[date]]  # resource_id -> shortfall dates from the impact result
    checks: list[ValidationCheck] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    touched_resources: set[str] = field(default_factory=set)
    offering_use: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    spare_use: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    order_problems: int = 0

    def add(self, code: CheckCode, result: CheckResult, detail: str, evidence: list[str] | None = None):
        self.checks.append(ValidationCheck(code=code, result=result, detail=detail, evidence_ids=evidence or []))
        if result == UNRESOLVED:
            self.need(detail)

    def need(self, text: str):
        if text not in self.unresolved:
            self.unresolved.append(text)

    def need_by(self, resource_id: str) -> date | None:
        dates = self.targets.get(resource_id)
        return min(dates) if dates else None

    def frozen_targets(self, resource_id: str) -> frozenset[date]:
        return frozenset(self.targets.get(resource_id, []))


def status_from_checks(checks: list[ValidationCheck]) -> ValidationStatus:
    results = {c.result for c in checks}
    if FAIL in results:
        return ValidationStatus.infeasible
    if UNRESOLVED in results:
        return ValidationStatus.needs_confirmation
    return ValidationStatus.feasible


def validate_plan(
    plan: RecoveryPlan,
    snapshot: BusinessSnapshot,
    impact: ImpactResult,
    incident: NormalizedIncident,
    *,
    validated_at: datetime | None = None,
) -> ValidationResult:
    """Validate one plan version. Re-run after every edit; never trust a stored status.

    ``incident`` is needed to know how the disruption changed the inbound
    deliveries (proposed addition to the §5 signature).
    """
    baseline = BusinessState.from_snapshot(snapshot, incident)
    targets: dict[str, list[date]] = defaultdict(list)
    for s in impact.resource_shortfalls:
        if s.shortfall > 0:
            targets[s.resource_id].append(s.date)
    run = _Run(state=baseline.copy(), baseline=baseline, incident=incident, targets=dict(targets))
    run.touched_resources.update(run.targets)

    for action in plan.actions:
        _apply_action(run, action)

    _check_coverage(run)
    _check_other_jobs(run)
    if run.order_problems == 0:
        run.add(CheckCode.DEPENDENCY_ORDER_OK, PASS, "All dates are today or later and in a possible order.")
    _check_facts(run, plan, snapshot, impact)

    return ValidationResult(
        plan_id=plan.id,
        plan_version=plan.version,
        status=status_from_checks(run.checks),
        checks=run.checks,
        unresolved_assumptions=run.unresolved,
        validated_at=validated_at or incident.reported_at,
    )


# ============================================================================ actions


def _apply_action(run: _Run, action: RecoveryAction) -> None:
    try:
        params = parse_parameters(action.action_type, action.parameters)
    except ParameterError as exc:
        run.add(CheckCode.FACTS_KNOWN, FAIL, f"{action.id}: invalid {action.action_type.value} parameters ({exc}).")
        return
    handler = {
        ActionType.use_spare_inventory: _use_spare,
        ActionType.source_alternative_supplier: _source_alternative,
        ActionType.reallocate_resource: _reallocate,
        ActionType.reschedule_job: _reschedule,
        ActionType.split_allocation: _split,
        ActionType.contact_supplier: _contact_supplier,
    }[action.action_type]
    handler(run, action, params)


def _require_resource(run: _Run, action: RecoveryAction, resource_id: str) -> bool:
    if run.state.resource(resource_id) is None:
        run.add(CheckCode.RESOURCE_AVAILABLE, FAIL, f"{action.id}: {resource_id} is not a known resource.")
        return False
    run.touched_resources.add(resource_id)
    return True


def _not_in_past(run: _Run, action: RecoveryAction, label: str, day: date) -> bool:
    if day < run.state.today:
        run.order_problems += 1
        run.add(
            CheckCode.DEPENDENCY_ORDER_OK, FAIL, f"{action.id}: {label} {day} is before today ({run.state.today})."
        )
        return False
    return True


def _use_spare(run: _Run, action: RecoveryAction, p: UseSpareInventory) -> None:
    if not _require_resource(run, action, p.resource_id):
        return
    spare = run.state.spare_quantity(p.resource_id)
    run.spare_use[p.resource_id] += p.quantity
    used = run.spare_use[p.resource_id]
    r = run.state.resource(p.resource_id)
    if used > spare:
        run.add(
            CheckCode.RESOURCE_AVAILABLE,
            FAIL,
            f"{action.id}: needs {used} spare {r.unit}s but records show {spare} "
            f"(owned {r.owned_quantity}, on hand {r.on_hand_quantity}).",
            [p.resource_id],
        )
        return
    where = f" at {p.location}" if p.location else ""
    run.add(
        CheckCode.RESOURCE_AVAILABLE,
        UNRESOLVED,
        f"{p.quantity} {r.name}{where} are recorded as owned but not on hand; physical availability is unverified.",
        [p.resource_id],
    )
    run.state.arrivals.append(Arrival(p.resource_id, run.state.today, p.quantity, action.id, plan_added=True))


def _source_alternative(run: _Run, action: RecoveryAction, p: SourceAlternativeSupplier) -> None:
    supplier = run.state.supplier(p.supplier_id)
    if supplier is None:
        run.add(
            CheckCode.SUPPLIER_AVAILABLE,
            FAIL,
            f"{action.id}: {p.supplier_id} is not a known supplier; availability cannot be assumed.",
        )
        return
    if not _require_resource(run, action, p.resource_id):
        return

    ok_order = _not_in_past(run, action, "order date", p.order_date)
    if p.expected_arrival_date < p.order_date:
        run.order_problems += 1
        ok_order = False
        run.add(
            CheckCode.DEPENDENCY_ORDER_OK,
            FAIL,
            f"{action.id}: arrival {p.expected_arrival_date} is before order date {p.order_date}.",
        )

    offers = run.state.offerings(p.supplier_id, p.resource_id)
    arrival_date = p.expected_arrival_date
    if not offers:
        run.add(
            CheckCode.SUPPLIER_AVAILABLE,
            UNRESOLVED,
            f"No recorded offer from {supplier.name} ({supplier.id}) for {p.resource_id}; "
            f"quantity, lead time and cost are unknown.",
            [supplier.id],
        )
        run.add(CheckCode.LEAD_TIME_OK, UNRESOLVED, f"Lead time for {supplier.id} / {p.resource_id} is not recorded.")
    else:
        offer = offers[0]
        run.offering_use[offer.id] += p.quantity
        promised = run.offering_use[offer.id]
        if offer.available_quantity is not None and promised > offer.available_quantity:
            run.add(
                CheckCode.SUPPLIER_AVAILABLE,
                FAIL,
                f"{supplier.id} has {offer.available_quantity} available; plan needs {promised}.",
                [offer.id],
            )
        elif not offer.availability_confirmed or offer.available_quantity is None:
            run.add(
                CheckCode.SUPPLIER_AVAILABLE,
                UNRESOLVED,
                f"{supplier.name} ({supplier.id}) has not confirmed {promised} available.",
                [offer.id],
            )
        else:
            run.add(
                CheckCode.SUPPLIER_AVAILABLE,
                PASS,
                f"{supplier.id} confirmed {offer.available_quantity} available.",
                [offer.id],
            )

        earliest = p.order_date + timedelta(days=offer.lead_time_days)
        need_by = run.need_by(p.resource_id)
        if p.expected_arrival_date < earliest:
            run.add(
                CheckCode.LEAD_TIME_OK,
                FAIL,
                f"{action.id} claims arrival {p.expected_arrival_date}, but a {offer.lead_time_days}-day lead "
                f"time from {p.order_date} means {earliest} at the earliest.",
                [offer.id],
            )
            arrival_date = earliest  # simulate what can really happen
        elif need_by is not None and earliest >= need_by:
            run.add(
                CheckCode.LEAD_TIME_OK,
                FAIL,
                f"{offer.lead_time_days}-day lead time; earliest arrival {earliest}, needed before {need_by}.",
                [offer.id],
            )
        elif ok_order:
            run.add(
                CheckCode.LEAD_TIME_OK,
                PASS,
                f"{offer.lead_time_days}-day lead time; arrives {p.expected_arrival_date}.",
                [offer.id],
            )

    run.state.arrivals.append(
        Arrival(p.resource_id, arrival_date, p.quantity, action.id, plan_added=True, persistent=False)
    )


def _job_demand(run: _Run, action: RecoveryAction, job_id: str, resource_id: str):
    if run.state.job(job_id) is None:
        run.add(CheckCode.RESOURCE_AVAILABLE, FAIL, f"{action.id}: {job_id} is not a known job.")
        return None
    demands = run.state.demands_for(resource_id, job_id)
    if not demands:
        run.add(CheckCode.RESOURCE_AVAILABLE, FAIL, f"{action.id}: {job_id} has no recorded need for {resource_id}.")
        return None
    return demands[0]


def _reallocate(run: _Run, action: RecoveryAction, p: ReallocateResource) -> None:
    if not _require_resource(run, action, p.resource_id):
        return
    src = _job_demand(run, action, p.from_job_id, p.resource_id)
    dst = _job_demand(run, action, p.to_job_id, p.resource_id)
    if src is None or dst is None:
        return
    if p.quantity > src.quantity:
        run.add(
            CheckCode.RESOURCE_AVAILABLE,
            FAIL,
            f"{action.id}: {p.from_job_id} is allocated {src.quantity}; cannot move {p.quantity}.",
            [src.dependency_id],
        )
        return
    # Taking units from a job only frees them for another job on a day the
    # source job would otherwise hold them; the pool maths decides that.
    src.quantity -= p.quantity
    run.add(
        CheckCode.RESOURCE_AVAILABLE,
        PASS,
        f"{p.from_job_id} holds {src.quantity + p.quantity}; {p.quantity} moved to {p.to_job_id}.",
        [src.dependency_id, dst.dependency_id],
    )


def _split(run: _Run, action: RecoveryAction, p: SplitAllocation) -> None:
    if not _require_resource(run, action, p.resource_id):
        return
    for alloc in p.allocations:
        d = _job_demand(run, action, alloc.job_id, p.resource_id)
        if d is not None:
            d.quantity = alloc.quantity


def _reschedule(run: _Run, action: RecoveryAction, p: RescheduleJob) -> None:
    job = run.state.job(p.job_id)
    if job is None:
        run.add(CheckCode.COMMITMENT_RESCHEDULABLE, FAIL, f"{action.id}: {p.job_id} is not a known job.")
        return
    commitments = run.state.commitments_for(job.id)
    fixed = [c for c in commitments if not c.reschedulable]
    if fixed:
        run.add(
            CheckCode.COMMITMENT_RESCHEDULABLE,
            FAIL,
            f"{job.id} fulfils {', '.join(c.id for c in fixed)}, recorded as not reschedulable.",
            [c.id for c in fixed],
        )
    elif not commitments:
        run.add(
            CheckCode.COMMITMENT_RESCHEDULABLE,
            UNRESOLVED,
            f"No commitment recorded for {job.id}; whether it may move is unknown.",
            [job.id],
        )
    else:
        run.add(
            CheckCode.COMMITMENT_RESCHEDULABLE,
            PASS,
            f"{', '.join(c.id for c in commitments)} recorded as reschedulable.",
            [c.id for c in commitments],
        )
    _not_in_past(run, action, "new date", p.new_date)
    for d in run.state.demands:
        if d.job_id == job.id:
            d.date = p.new_date
            run.touched_resources.add(d.resource_id)


def _contact_supplier(run: _Run, action: RecoveryAction, p: ContactSupplier) -> None:
    supplier = run.state.supplier(p.supplier_id)
    if supplier is None:
        run.add(CheckCode.SUPPLIER_AVAILABLE, FAIL, f"{action.id}: {p.supplier_id} is not a known supplier.")
        return
    if p.requested_date is not None:
        _not_in_past(run, action, "requested date", p.requested_date)

    offer_ids = [o.id for o in run.state.offerings(supplier.id)]
    delayed = run.state.delayed_arrival
    is_expedite = (
        supplier.id == run.incident.supplier_id
        and delayed is not None
        and p.requested_date is not None
        and (p.resource_id is None or p.resource_id == delayed.resource_id)
    )
    if is_expedite:
        qty = min(p.quantity or delayed.quantity, delayed.quantity)
        run.add(
            CheckCode.SUPPLIER_AVAILABLE,
            UNRESOLVED,
            f"{supplier.id} has not confirmed it can deliver {qty} by {p.requested_date}.",
            offer_ids,
        )
        # Simulate the outcome *if* the supplier agrees, so a request that
        # would not help even when granted is still caught as infeasible.
        delayed.quantity -= qty
        run.state.arrivals.append(
            Arrival(delayed.resource_id, p.requested_date, qty, action.id, plan_added=True)
        )
        run.touched_resources.add(delayed.resource_id)
    elif p.quantity and p.resource_id and p.requested_date is not None:
        run.add(
            CheckCode.SUPPLIER_AVAILABLE,
            UNRESOLVED,
            f"Asking {supplier.id} for {p.quantity} {p.resource_id} by {p.requested_date}; not confirmed.",
            offer_ids,
        )
        run.state.arrivals.append(
            Arrival(p.resource_id, p.requested_date, p.quantity, action.id, plan_added=True, persistent=False)
        )
        run.touched_resources.add(p.resource_id)
    else:
        run.add(
            CheckCode.SUPPLIER_AVAILABLE,
            UNRESOLVED,
            f"Information request to {supplier.id}; adds no confirmed supply.",
            offer_ids,
        )


# ============================================================================ global checks


def _check_coverage(run: _Run) -> None:
    if not run.targets:
        run.add(CheckCode.QUANTITY_SUFFICIENT, PASS, "Impact analysis reports no shortfall to cover.")
        return
    for resource_id, dates in sorted(run.targets.items()):
        targets = run.frozen_targets(resource_id)
        for day in sorted(dates):
            demands = [d for d in run.state.demands_for(resource_id) if d.date == day]
            evidence = sorted({d.dependency_id for d in demands} | {resource_id})
            required = sum(d.quantity for d in demands)
            base = run.baseline.pool(resource_id, day)
            counted = run.state.pool(resource_id, day, target_dates=targets, ignore_plan_timing=True)
            timed = run.state.pool(resource_id, day, target_dates=targets)
            added = counted - base

            sums = f"{base} + {added} = {counted}" if added else f"{counted}"
            if counted >= required:
                run.add(
                    CheckCode.QUANTITY_SUFFICIENT,
                    PASS,
                    f"{sums} meets {required} required on {day}.",
                    evidence,
                )
            else:
                run.add(
                    CheckCode.QUANTITY_SUFFICIENT,
                    FAIL,
                    f"{sums} against {required} required on {day}; short {required - counted}.",
                    evidence,
                )

            late = [
                a
                for a in run.state.arrivals
                if a.plan_added and a.resource_id == resource_id and a.date >= day
            ]
            if timed >= min(required, counted):
                if added:
                    run.add(CheckCode.DEADLINE_MET, PASS, f"Plan supply arrives before {day}.", evidence)
            else:
                when = ", ".join(f"{a.source_id} arrives {a.date}" for a in late) or "plan supply arrives too late"
                run.add(
                    CheckCode.DEADLINE_MET,
                    FAIL,
                    f"{when}; units must arrive before {day} (setup day).",
                    evidence + sorted({a.source_id for a in late}),
                )

    # Jobs left with less than their recorded requirement (split / reallocate).
    for d in run.state.demands:
        if d.quantity < d.required:
            run.add(
                CheckCode.QUANTITY_SUFFICIENT,
                UNRESOLVED,
                f"{d.job_id} would get {d.quantity} of {d.required} {d.resource_id} required; "
                f"needs the customer's agreement to partial fulfilment.",
                [d.dependency_id],
            )


def _check_other_jobs(run: _Run) -> None:
    """NO_DOUBLE_BOOKING: days other than the shortfall days must not get worse."""
    broken: list[str] = []
    evidence: set[str] = set()
    checked: set[date] = set()
    for resource_id in sorted(run.touched_resources):
        targets = run.frozen_targets(resource_id)
        days = (run.state.demand_dates(resource_id) | run.baseline.demand_dates(resource_id)) - targets
        for day in sorted(days):
            checked.add(day)
            after_gap = run.state.demand_on(resource_id, day) - run.state.pool(resource_id, day, target_dates=targets)
            before_gap = run.baseline.demand_on(resource_id, day) - run.baseline.pool(resource_id, day)
            if after_gap > 0 and after_gap > before_gap:
                jobs = [d for d in run.state.demands_for(resource_id) if d.date == day]
                evidence.update(d.dependency_id for d in jobs)
                broken.append(
                    f"{', '.join(d.job_id for d in jobs)} on {day} would be short {after_gap} {resource_id}"
                )
    if broken:
        run.add(CheckCode.NO_DOUBLE_BOOKING, FAIL, "; ".join(broken) + ".", sorted(evidence))
    else:
        days = ", ".join(str(d) for d in sorted(checked)) or "none"
        run.add(CheckCode.NO_DOUBLE_BOOKING, PASS, f"No other job loses coverage (other days checked: {days}).")


def _check_facts(run: _Run, plan: RecoveryPlan, snapshot: BusinessSnapshot, impact: ImpactResult) -> None:
    for text in plan.assumptions:
        run.need(text)

    for s in impact.resource_shortfalls:
        computed = run.baseline.pool(s.resource_id, s.date)
        if computed != s.available:
            run.need(
                f"Impact analysis reports {s.available} {s.resource_id} available on {s.date}, "
                f"but snapshot + incident give {computed}."
            )
    for fact in impact.unresolved_facts:
        run.need(f"Impact analysis has an unresolved fact: {fact}")
    for note in run.baseline.notes:
        run.need(note)

    for resource_id in sorted(run.touched_resources):
        r = run.state.resource(resource_id)
        if r is not None and not r.on_hand_verified:
            run.need(f"On-hand count of {r.name} ({r.id}) is not verified.")

    touched_ids = _plan_entity_ids(plan) | run.touched_resources
    for item in snapshot.missing_or_stale:
        # Items naming an entity only matter if the plan uses it; anything we
        # cannot attribute is treated as relevant rather than ignored.
        entity_id = item.get("entity_id") if isinstance(item, dict) else None
        if entity_id is None or entity_id in touched_ids:
            run.need(f"Snapshot marks data missing or stale: {item}")

    if run.unresolved:
        run.checks.append(
            ValidationCheck(
                code=CheckCode.FACTS_KNOWN,
                result=UNRESOLVED,
                detail=f"Depends on {len(run.unresolved)} unconfirmed fact(s).",
                evidence_ids=[],
            )
        )
    else:
        run.add(CheckCode.FACTS_KNOWN, PASS, "All inputs confirmed.")


def _plan_entity_ids(plan: RecoveryPlan) -> set[str]:
    ids: set[str] = set()
    for a in plan.actions:
        ids.add(a.target_entity.entity_id)
        for key, value in a.parameters.items():
            if key.endswith("_id") and isinstance(value, str):
                ids.add(value)
    return ids
