"""Estimated *additional purchase cost* of a plan, from recorded unit costs only.

``None`` means unknown and is never replaced by 0 (CONTRACTS.md §2):

- source_alternative_supplier: quantity x the offer's recorded ``unit_cost``;
  unknown if there is no offer or no unit cost.
- contact_supplier: unknown (the supplier may charge for expediting).
- use_spare_inventory, reallocate_resource, split_allocation, reschedule_job:
  0 — nothing is bought. Operational effort is reflected in the ranking's
  "changes required" factor, not here.

One unknown action makes the whole plan's cost unknown.
"""

from __future__ import annotations

from decimal import Decimal

from .actions import ParameterError, SourceAlternativeSupplier, parse_parameters
from .contracts import ActionType, BusinessSnapshot, Money, RecoveryPlan

NO_PURCHASE = {
    ActionType.use_spare_inventory,
    ActionType.reallocate_resource,
    ActionType.split_allocation,
    ActionType.reschedule_job,
}


def estimate_cost(plan: RecoveryPlan, snapshot: BusinessSnapshot) -> Money | None:
    currency = snapshot.business.currency
    total = Decimal("0")
    for action in plan.actions:
        if action.action_type in NO_PURCHASE:
            continue
        if action.action_type != ActionType.source_alternative_supplier:
            return None
        try:
            p: SourceAlternativeSupplier = parse_parameters(action.action_type, action.parameters)
        except ParameterError:
            return None
        offer = next(
            (
                o
                for o in snapshot.supplier_offerings
                if o.supplier_id == p.supplier_id and o.resource_id == p.resource_id
            ),
            None,
        )
        if offer is None or offer.unit_cost is None or offer.unit_cost.currency != currency:
            return None
        total += offer.unit_cost.amount * p.quantity
    return Money(amount=total.quantize(Decimal("0.01")), currency=currency)
