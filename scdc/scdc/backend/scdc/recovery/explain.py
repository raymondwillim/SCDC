"""Stage 6: structured, template-generated explanation of every plan.

Built only from the plan, its ValidationResult and the snapshot, so it cannot
state anything the validator did not establish. P5 may rephrase this for
people; ``validation_status`` must be copied, never regenerated.
"""

from __future__ import annotations

from pydantic import BaseModel

from .contracts import ActionType, BusinessSnapshot, CheckResult, RecoveryPlan, ValidationCheck, ValidationStatus
from .ranking import PlanScore


class PlanExplanation(BaseModel):
    plan_id: str
    plan_version: int
    summary: str
    validation_status: ValidationStatus
    rank: int | None
    proposed_actions: list[str]
    passed_checks: list[ValidationCheck]
    failed_checks: list[ValidationCheck]
    unresolved_checks: list[ValidationCheck]
    benefits: list[str]
    trade_offs: list[str]
    required_confirmations: list[str]
    assumptions: list[str]
    supporting_facts: list[str]
    ranking_reason: str
    generated_by: str = "template"


def _describe_action(action, snapshot: BusinessSnapshot) -> str:
    p = action.parameters
    names = {s.id: s.name for s in snapshot.suppliers} | {j.id: j.name for j in snapshot.jobs}
    names |= {r.id: r.name for r in snapshot.resources}

    def n(key):
        value = p.get(key)
        return f"{names[value]} ({value})" if value in names else str(value)

    t = action.action_type
    if t == ActionType.source_alternative_supplier:
        return (
            f"Order {p.get('quantity')} x {n('resource_id')} from {n('supplier_id')} on {p.get('order_date')}, "
            f"arriving {p.get('expected_arrival_date')}."
        )
    if t == ActionType.use_spare_inventory:
        return f"Take {p.get('quantity')} x {n('resource_id')} from spare stock."
    if t == ActionType.reallocate_resource:
        return f"Move {p.get('quantity')} x {n('resource_id')} from {n('from_job_id')} to {n('to_job_id')}."
    if t == ActionType.reschedule_job:
        return f"Reschedule {n('job_id')} to {p.get('new_date')}."
    if t == ActionType.split_allocation:
        parts = ", ".join(f"{a.get('job_id')}: {a.get('quantity')}" for a in p.get("allocations", []))
        return f"Split {n('resource_id')} as {parts}."
    return f"Ask {n('supplier_id')}: \"{p.get('request')}\" (by {p.get('requested_date') or 'n/a'})."


def explain_plan(plan: RecoveryPlan, snapshot: BusinessSnapshot, score: PlanScore | None = None) -> PlanExplanation:
    v = plan.validation
    if v is None:
        raise ValueError(f"{plan.id} has not been validated.")
    by = {r: [c for c in v.checks if c.result == r] for r in CheckResult}
    status = v.status

    benefits, trade_offs = [], []
    if status != ValidationStatus.infeasible:
        if any(c.code.value == "QUANTITY_SUFFICIENT" and c.result == CheckResult.pass_ for c in v.checks):
            benefits.append("Covers the full shortfall on every affected day.")
        if not any(a.action_type == ActionType.reschedule_job for a in plan.actions):
            benefits.append("Keeps every event on its original date.")
        if plan.estimated_cost is not None:
            if plan.estimated_cost.amount == 0:
                benefits.append("No additional purchase cost.")
            else:
                trade_offs.append(
                    f"Additional cost {plan.estimated_cost.currency} {plan.estimated_cost.amount:.2f} "
                    f"(from recorded unit prices)."
                )
        else:
            trade_offs.append("Cost is unknown; no price is recorded for at least one action.")
        if len(plan.actions) > 1:
            trade_offs.append(f"Requires {len(plan.actions)} coordinated actions.")
        if any(a.action_type == ActionType.reschedule_job for a in plan.actions):
            trade_offs.append("Moves a customer event; the customer must be informed.")
    if status == ValidationStatus.needs_confirmation:
        trade_offs.append("Not validated until the confirmations below are obtained.")
    if status == ValidationStatus.infeasible:
        trade_offs += [f"Rejected: {c.detail}" for c in by[CheckResult.fail]]

    facts = sorted({f"{c.code.value}: {', '.join(c.evidence_ids)}" for c in v.checks if c.evidence_ids})
    return PlanExplanation(
        plan_id=plan.id,
        plan_version=plan.version,
        summary=plan.summary,
        validation_status=status,
        rank=plan.rank,
        proposed_actions=[_describe_action(a, snapshot) for a in plan.actions],
        passed_checks=by[CheckResult.pass_],
        failed_checks=by[CheckResult.fail],
        unresolved_checks=by[CheckResult.unresolved],
        benefits=benefits,
        trade_offs=trade_offs,
        required_confirmations=list(v.unresolved_assumptions),
        assumptions=list(plan.assumptions),
        supporting_facts=facts,
        ranking_reason=score.reason if score else "",
    )
