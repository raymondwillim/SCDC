from scdc.recovery.contracts import (
    ActionType,
    CheckResult,
    EntityType,
    RecoveryAction,
    RecoveryPlan,
    TargetEntity,
)


def action(action_type: str, params: dict, n: int = 1, target=("resource", "R-01")) -> RecoveryAction:
    return RecoveryAction(
        id=f"RA-9{n:02d}",
        action_type=ActionType(action_type),
        target_entity=TargetEntity(entity_type=EntityType(target[0]), entity_id=target[1]),
        parameters=params,
    )


def plan(*actions: RecoveryAction, assumptions=None, id_="RP-900") -> RecoveryPlan:
    return RecoveryPlan(
        id=id_, disruption_id="D-001", summary="test plan", actions=list(actions), assumptions=assumptions or []
    )


def rent(supplier="S-02", qty=20, order="2026-10-20", arrive="2026-10-21", n=1) -> RecoveryAction:
    return action(
        "source_alternative_supplier",
        {"supplier_id": supplier, "resource_id": "R-01", "quantity": qty, "order_date": order,
         "expected_arrival_date": arrive},
        n,
    )


def results(validation) -> dict[str, set[str]]:
    """code -> set of results, e.g. {"DEADLINE_MET": {"fail"}}."""
    out: dict[str, set[str]] = {}
    for c in validation.checks:
        out.setdefault(c.code.value, set()).add(c.result.value)
    return out


def failed_codes(validation) -> set[str]:
    return {c.code.value for c in validation.checks if c.result == CheckResult.fail}
