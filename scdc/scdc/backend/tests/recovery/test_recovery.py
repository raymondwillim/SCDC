"""P4 tests: generation, deterministic validation, ranking, editing.

Run from backend/:  python -m pytest tests/recovery -q
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scdc.recovery import (
    IdFactory,
    RankingWeights,
    RecoveryInputError,
    StalePlanVersion,
    StaticSuggester,
    edit_plan,
    generate_candidates,
    plan_recovery,
    rank_plans,
    validate_plan,
)
from scdc.recovery.contracts import PlanOrigin, PlanStatus, ValidationStatus

from tests.recovery.conftest import by_id
from tests.recovery.helpers import action, failed_codes, plan, rent, results

MYT = timezone(timedelta(hours=8))
FEASIBLE, INFEASIBLE, NEEDS = (
    ValidationStatus.feasible,
    ValidationStatus.infeasible,
    ValidationStatus.needs_confirmation,
)


# ============================================================================ demo scenario (§9)


def test_demo_matches_expected_fixture(snapshot, incident, impact, expected_plans):
    result = plan_recovery(incident, snapshot, impact)
    got = {p.id: p for p in result.plans}
    assert set(got) == {p["id"] for p in expected_plans["plans"]}

    for exp in expected_plans["plans"]:
        p = got[exp["id"]]
        assert p.validation.status.value == exp["validation"]["status"], p.id
        exp_failed = {c["code"] for c in exp["validation"]["checks"] if c["result"] == "fail"}
        assert failed_codes(p.validation) == exp_failed, p.id
        assert p.rank == exp["rank"], p.id
        assert p.to_contract()["estimated_cost"] == exp["estimated_cost"], p.id
        assert [a.model_dump(mode="json") for a in p.actions] == exp["actions"], p.id
        assert p.assumptions == exp["assumptions"], p.id

    assert result.recommended.id == "RP-001"
    assert [p.id for p in result.plans] == ["RP-001", "RP-003", "RP-002"]  # feasible, pending, rejected


def test_valid_plan_is_feasible_with_explained_checks(snapshot, incident, impact):
    v = validate_plan(plan(rent("S-02", 20)), snapshot, impact, incident)
    assert v.status == FEASIBLE
    assert all(r == {"pass"} for r in results(v).values())
    qty = next(c for c in v.checks if c.code.value == "QUANTITY_SUFFICIENT")
    assert qty.detail.startswith("50 + 20 = 70 meets 70")
    assert v.unresolved_assumptions == []


def test_expedite_request_needs_confirmation(snapshot, incident, impact):
    result = plan_recovery(incident, snapshot, impact)
    rp3 = next(p for p in result.plans if p.id == "RP-003")
    assert rp3.validation.status == NEEDS
    assert results(rp3.validation)["SUPPLIER_AVAILABLE"] == {"unresolved"}
    assert "S-01 can expedite the remaining 20 chairs to 2026-10-21." in rp3.validation.unresolved_assumptions
    expl = next(e for e in result.explanations if e.plan_id == "RP-003")
    assert expl.required_confirmations and expl.rank is None


# ============================================================================ insufficient stock


def test_spare_inventory_that_does_not_exist_is_infeasible(snapshot, incident, impact):
    # Seed: owned 30 == on hand 30 -> no spare stock exists.
    p = plan(action("use_spare_inventory", {"resource_id": "R-01", "quantity": 20}))
    v = validate_plan(p, snapshot, impact, incident)
    assert v.status == INFEASIBLE
    assert "RESOURCE_AVAILABLE" in failed_codes(v)


def test_supplier_with_too_little_stock_is_infeasible(make_snapshot, incident, impact):
    snap = make_snapshot(lambda s: by_id(s["supplier_offerings"], "SO-02").update(available_quantity=10))
    v = validate_plan(plan(rent("S-02", 20)), snap, impact, incident)
    assert v.status == INFEASIBLE
    assert "SUPPLIER_AVAILABLE" in failed_codes(v)


def test_ordering_too_few_units_is_infeasible(snapshot, incident, impact):
    v = validate_plan(plan(rent("S-02", 15)), snapshot, impact, incident)
    assert failed_codes(v) == {"QUANTITY_SUFFICIENT"}
    assert "short 5" in next(c.detail for c in v.checks if c.result.value == "fail")


# ============================================================================ deadline violation


def test_slow_supplier_violates_deadline(snapshot, incident, impact):
    v = validate_plan(plan(rent("S-03", 20, arrive="2026-10-23")), snapshot, impact, incident)
    assert v.status == INFEASIBLE
    assert failed_codes(v) == {"LEAD_TIME_OK", "DEADLINE_MET"}


def test_claiming_faster_than_lead_time_is_caught(snapshot, incident, impact):
    # S-03 has a 3-day lead time; the plan pretends it arrives in 1 day.
    v = validate_plan(plan(rent("S-03", 20, arrive="2026-10-21")), snapshot, impact, incident)
    assert v.status == INFEASIBLE
    assert {"LEAD_TIME_OK", "DEADLINE_MET"} <= failed_codes(v)


def test_arrival_on_event_day_is_too_late(make_snapshot, incident, impact):
    snap = make_snapshot(lambda s: by_id(s["supplier_offerings"], "SO-02").update(lead_time_days=2))
    v = validate_plan(plan(rent("S-02", 20, arrive="2026-10-22")), snap, impact, incident)
    assert "DEADLINE_MET" in failed_codes(v)


def test_ordering_in_the_past_is_rejected(snapshot, incident, impact):
    v = validate_plan(plan(rent("S-02", 20, order="2026-10-19", arrive="2026-10-21")), snapshot, impact, incident)
    assert "DEPENDENCY_ORDER_OK" in failed_codes(v)


# ============================================================================ double booking


def test_same_offer_promised_twice_is_double_booking(snapshot, incident, impact):
    # S-02 has 25 chairs; two actions take 20 + 20.
    p = plan(rent("S-02", 20, n=1), rent("S-02", 20, n=2))
    v = validate_plan(p, snapshot, impact, incident)
    assert v.status == INFEASIBLE
    assert "SUPPLIER_AVAILABLE" in failed_codes(v)


def test_moving_a_job_onto_a_full_day_breaks_another_job(make_snapshot, incident, impact):
    def add_job(s):
        s["jobs"].append({"id": "J-04", "name": "Office lunch", "event_date": "2026-10-21",
                          "deadline": "2026-10-21", "status": "scheduled", "priority": "medium"})
        s["dependencies"].append({"id": "DEP-10", "source_type": "resource", "source_id": "R-01",
                                  "target_type": "job", "target_id": "J-04", "dependency_type": "requires",
                                  "required_quantity": 30, "required_by": "2026-10-21"})

    snap = make_snapshot(add_job)
    # Covers the 22 Oct shortfall, but also moves J-03 (reschedulable) onto 21 Oct,
    # when all 30 chairs on hand are already booked for J-04.
    p = plan(rent("S-02", 20), action("reschedule_job", {"job_id": "J-03", "new_date": "2026-10-21"}, 2,
                                      target=("job", "J-03")))
    v = validate_plan(p, snap, impact, incident)
    assert v.status == INFEASIBLE
    assert failed_codes(v) == {"NO_DOUBLE_BOOKING"}
    assert results(v)["COMMITMENT_RESCHEDULABLE"] == {"pass"}


def test_rescheduling_a_fixed_commitment_is_rejected(snapshot, incident, impact):
    p = plan(action("reschedule_job", {"job_id": "J-02", "new_date": "2026-10-24"}, target=("job", "J-02")))
    v = validate_plan(p, snapshot, impact, incident)
    assert "COMMITMENT_RESCHEDULABLE" in failed_codes(v)


# ============================================================================ unknown alternative supplier


def test_unknown_supplier_is_never_assumed_available(snapshot, incident, impact):
    v = validate_plan(plan(rent("S-99", 20)), snapshot, impact, incident)
    assert v.status == INFEASIBLE
    assert "SUPPLIER_AVAILABLE" in failed_codes(v)


def test_known_supplier_without_recorded_offer_needs_confirmation(make_snapshot, incident, impact):
    snap = make_snapshot(lambda s: s["suppliers"].append({"id": "S-04", "name": "Kedai Baru"}))
    v = validate_plan(plan(rent("S-04", 20)), snap, impact, incident)
    assert v.status == NEEDS
    assert results(v)["SUPPLIER_AVAILABLE"] == {"unresolved"}


def test_unconfirmed_availability_is_not_feasible(make_snapshot, incident, impact):
    snap = make_snapshot(lambda s: by_id(s["supplier_offerings"], "SO-02").update(availability_confirmed=False))
    v = validate_plan(plan(rent("S-02", 20)), snap, impact, incident)
    assert v.status == NEEDS


# ============================================================================ no feasible option


def test_no_feasible_recovery_option(make_snapshot, incident, impact):
    snap = make_snapshot(lambda s: by_id(s["supplier_offerings"], "SO-02").update(available_quantity=5))
    result = plan_recovery(incident, snap, impact)
    assert result.no_feasible_plan
    assert all(p.rank is None for p in result.plans)
    assert all(p.validation.status != FEASIBLE for p in result.plans)
    # The expedite request is the only remaining path and is clearly marked as unconfirmed.
    assert any("Confirm" in step for step in result.next_steps())


def test_nothing_recorded_at_all(make_snapshot, incident, impact):
    def strip(s):
        s["supplier_offerings"] = [o for o in s["supplier_offerings"] if o["supplier_id"] == "S-01"]

    # Reported on the event morning: no alternative offers, and too late to ask for an expedite.
    late_incident = incident.model_copy(update={"reported_at": datetime(2026, 10, 22, 7, 0, tzinfo=MYT)})
    result = plan_recovery(late_incident, make_snapshot(strip), impact)
    assert result.no_feasible_plan
    # The only option left: keep the 50 chairs on hand for the wedding (high priority).
    assert len(result.plans) == 1
    only = result.plans[0]
    assert only.actions[0].action_type.value == "split_allocation"
    assert only.actions[0].parameters["allocations"] == [
        {"job_id": "J-01", "quantity": 50}, {"job_id": "J-02", "quantity": 0}]
    assert only.validation.status == NEEDS
    assert any("accepts 0 of 20" in step for step in result.next_steps())


# ============================================================================ editing


def test_edit_makes_plan_infeasible(snapshot, incident, impact, expected_plans):
    scenario = expected_plans["edit_scenario"]
    rp1 = next(p for p in plan_recovery(incident, snapshot, impact).plans if p.id == scenario["plan_id"])
    actions = [a.model_dump(mode="json") for a in rp1.actions]
    actions[0]["parameters"]["quantity"] = scenario["changed_parameter"]["quantity"]

    old, new = edit_plan(rp1, base_version=scenario["base_version"], actions=actions, assumptions=None,
                         snapshot=snapshot, impact=impact, incident=incident)
    assert old.status == PlanStatus.superseded
    assert new.version == scenario["new_version"]
    assert new.created_by == PlanOrigin.user_edit
    assert new.validation.status.value == scenario["expected_status"]
    assert failed_codes(new.validation) == set(scenario["expected_failed_checks"])
    assert new.rank is None
    assert new.estimated_cost.amount == Decimal("40.00")  # re-costed, not copied


def test_edit_on_stale_version_is_refused(snapshot, incident, impact):
    rp1 = plan_recovery(incident, snapshot, impact).plans[0]
    with pytest.raises(StalePlanVersion):
        edit_plan(rp1, base_version=0, actions=[a.model_dump() for a in rp1.actions], assumptions=None,
                  snapshot=snapshot, impact=impact, incident=incident)


def test_edit_adding_an_assumption_needs_confirmation(snapshot, incident, impact):
    rp1 = plan_recovery(incident, snapshot, impact).plans[0]
    _, new = edit_plan(rp1, base_version=1, actions=[a.model_dump() for a in rp1.actions],
                       assumptions=["Cahaya can deliver to the hall directly."],
                       snapshot=snapshot, impact=impact, incident=incident)
    assert new.validation.status == NEEDS


def test_malformed_edit_is_reported_not_crashed(snapshot, incident, impact):
    bad = plan(action("source_alternative_supplier", {"supplier_id": "S-02", "quantity": "lots"}))
    v = validate_plan(bad, snapshot, impact, incident)
    assert v.status == INFEASIBLE
    assert "invalid" in v.checks[0].detail


# ============================================================================ multiple feasible plans


@pytest.fixture
def richer_snapshot(make_snapshot):
    def mutate(s):
        by_id(s["supplier_offerings"], "SO-03").update(lead_time_days=1)  # Delima now fast and cheaper
        by_id(s["commitments"], "CM-02").update(reschedulable=True)  # seminar may move

    return make_snapshot(mutate)


def test_multiple_feasible_plans_ranked_by_trade_offs(richer_snapshot, incident, impact):
    result = plan_recovery(incident, richer_snapshot, impact)
    feasible = [p for p in result.plans if p.validation.status == FEASIBLE]
    assert len(feasible) == 3
    ranked = {p.rank: p for p in feasible}
    # Cheapest purchase that keeps every date wins; rescheduling is free but moves a customer.
    assert ranked[1].actions[0].parameters["supplier_id"] == "S-03"
    assert ranked[1].estimated_cost.amount == Decimal("56.00")
    assert ranked[2].actions[0].parameters["supplier_id"] == "S-02"
    assert ranked[3].actions[0].action_type.value == "reschedule_job"
    resched = next(e for e in result.explanations if e.rank == 3)
    assert any("customer" in t for t in resched.trade_offs)
    assert all(e.ranking_reason for e in result.explanations)


def test_ranking_weights_are_configurable(richer_snapshot, incident, impact):
    cost_only = RankingWeights(protection=0, deadlines=0, cost=1, simplicity=0, certainty=0)
    result = plan_recovery(incident, richer_snapshot, impact, weights=cost_only)
    assert result.recommended.actions[0].action_type.value == "reschedule_job"  # RM 0.00


def test_rank_plans_requires_validation(snapshot, incident, impact):
    plans = generate_candidates(incident, snapshot, impact).plans
    with pytest.raises(ValueError):
        rank_plans(plans)


# ============================================================================ guards


def test_incident_needing_clarification_is_not_planned(incident, snapshot, impact):
    unclear = incident.model_copy(update={"quantity_affected": None})
    with pytest.raises(RecoveryInputError):
        plan_recovery(unclear, snapshot, impact)


def test_unverified_on_hand_stock_needs_confirmation(make_snapshot, incident, impact):
    snap = make_snapshot(lambda s: by_id(s["resources"], "R-01").update(on_hand_verified=False))
    v = validate_plan(plan(rent("S-02", 20)), snap, impact, incident)
    assert v.status == NEEDS


def test_impact_result_disagreeing_with_snapshot_is_flagged(snapshot, incident, impact):
    wrong = impact.model_copy(deep=True)
    wrong.resource_shortfalls[0].available = 55
    v = validate_plan(plan(rent("S-02", 20)), snapshot, wrong, incident)
    assert v.status == NEEDS
    assert any("Impact analysis reports 55" in u for u in v.unresolved_assumptions)


def test_results_are_deterministic(snapshot, incident, impact):
    a = plan_recovery(incident, snapshot, impact)
    b = plan_recovery(incident, snapshot, impact)
    assert [p.model_dump() for p in a.plans] == [p.model_dump() for p in b.plans]


def test_ids_continue_from_factory(snapshot, incident, impact):
    plans = generate_candidates(incident, snapshot, impact, ids=IdFactory(next_plan=7, next_action=12)).plans
    assert [p.id for p in plans] == ["RP-007", "RP-008", "RP-009"]
    assert plans[0].actions[0].id == "RA-012"


# ============================================================================ AI adapter


def test_ai_suggestions_go_through_the_same_validation(snapshot, incident, impact):
    suggester = StaticSuggester([
        {   # hallucinated supplier, with a self-declared status the module must ignore
            "summary": "Borrow chairs from Mega Chairs Sdn Bhd.",
            "feasible": True,
            "actions": [{"action_type": "source_alternative_supplier",
                         "target_entity": {"entity_type": "resource", "entity_id": "R-01"},
                         "parameters": {"supplier_id": "S-77", "resource_id": "R-01", "quantity": 20,
                                        "order_date": "2026-10-20", "expected_arrival_date": "2026-10-21"}}],
        },
        {"summary": "Just buy more", "actions": [{"action_type": "teleport_chairs"}]},  # malformed
    ])
    result = plan_recovery(incident, snapshot, impact, suggester=suggester)
    ai = [p for p in result.plans if p.created_by == PlanOrigin.ai_suggestion]
    assert len(ai) == 1 and ai[0].validation.status == INFEASIBLE
    assert any("rejected" in w for w in result.warnings)
    assert result.recommended.id == "RP-001"


def test_ai_failure_falls_back_to_rules(snapshot, incident, impact):
    class Broken:
        name = "broken"

        def suggest(self, *args):
            raise TimeoutError("model timeout")

    result = plan_recovery(incident, snapshot, impact, suggester=Broken())
    assert len(result.plans) == 3
    assert any("failed" in w for w in result.warnings)


# ============================================================================ other lateness amounts


def _late(incident, impact, late: int):
    """Same delivery, but ``late`` of the 40 chairs slip to 23 Oct."""
    inc = incident.model_copy(update={"quantity_affected": late})
    imp = impact.model_copy(deep=True)
    s = imp.resource_shortfalls[0]
    s.available = 30 + 40 - late
    s.shortfall = max(0, 70 - s.available)
    return inc, imp


def test_only_five_chairs_late(snapshot, incident, impact):
    inc, imp = _late(incident, impact, 5)
    result = plan_recovery(inc, snapshot, imp)
    best = result.recommended
    assert best.actions[0].parameters == {
        "supplier_id": "S-02", "resource_id": "R-01", "quantity": 5,
        "order_date": "2026-10-20", "expected_arrival_date": "2026-10-21",
    }
    assert best.validation.status == FEASIBLE
    assert best.estimated_cost.amount == Decimal("20.00")
    qty = next(c for c in best.validation.checks if c.code.value == "QUANTITY_SUFFICIENT")
    assert qty.detail.startswith("65 + 5 = 70 meets 70")
    expedite = next(p for p in result.plans if p.actions[0].action_type.value == "contact_supplier")
    assert "remaining 5 chairs" in expedite.summary


def test_supplier_shortage_reports_true_chair_count(snapshot, incident, impact):
    # 40 late: Cahaya has only 25, so "rent 40 from Cahaya" must count 25, not 40.
    inc, imp = _late(incident, impact, 40)
    v = validate_plan(plan(rent("S-02", 40)), snapshot, imp, inc)
    assert v.status == INFEASIBLE
    qty = next(c for c in v.checks if c.code.value == "QUANTITY_SUFFICIENT")
    assert qty.result.value == "fail"
    assert qty.detail.startswith("30 + 25 = 55 against 70") and "short 15" in qty.detail


def test_missing_spare_stock_is_not_counted(make_snapshot, incident, impact):
    snap = make_snapshot(lambda s: by_id(s["resources"], "R-01").update(owned_quantity=40))  # 10 spare
    p = plan(action("use_spare_inventory", {"resource_id": "R-01", "quantity": 20}))
    v = validate_plan(p, snap, impact, incident)
    qty = next(c for c in v.checks if c.code.value == "QUANTITY_SUFFICIENT")
    assert qty.detail.startswith("50 + 10 = 60") and "short 10" in qty.detail


def test_partial_supply_plus_expedite_when_nobody_has_enough(snapshot, incident, impact):
    inc, imp = _late(incident, impact, 40)
    result = plan_recovery(inc, snapshot, imp)
    assert result.no_feasible_plan  # only 25 chairs can arrive in time from confirmed sources
    combo = next(p for p in result.plans if [a.action_type.value for a in p.actions]
                 == ["source_alternative_supplier", "contact_supplier"])
    assert combo.actions[0].parameters["supplier_id"] == "S-02"
    assert combo.actions[0].parameters["quantity"] == 25
    assert combo.actions[1].parameters["quantity"] == 15  # Maju only rushes the rest
    assert combo.validation.status == NEEDS
    assert failed_codes(combo.validation) == set()
    qty = next(c for c in combo.validation.checks if c.code.value == "QUANTITY_SUFFICIENT")
    assert qty.detail.startswith("30 + 40 = 70 meets 70")


def test_no_partial_expedite_plan_in_demo(snapshot, incident, impact):
    # Cahaya alone covers the demo gap, so the extra template must stay quiet.
    assert len(plan_recovery(incident, snapshot, impact).plans) == 3


# ============================================================================ re-check fixes (24 Oct)


def test_demo_lists_each_confirmation_once(snapshot, incident, impact, expected_plans):
    rp3 = next(p for p in plan_recovery(incident, snapshot, impact).plans if p.id == "RP-003")
    exp = next(p for p in expected_plans["plans"] if p["id"] == "RP-003")
    assert rp3.validation.unresolved_assumptions == exp["validation"]["unresolved_assumptions"]


def test_old_supplier_confirmation_must_be_reconfirmed(make_snapshot, incident, impact):
    snap = make_snapshot(lambda s: by_id(s["supplier_offerings"], "SO-02").update(
        confirmed_at="2026-09-01T09:00:00+08:00"))
    v = validate_plan(plan(rent("S-02", 20)), snap, impact, incident)
    assert v.status == NEEDS
    assert any("re-confirm" in u for u in v.unresolved_assumptions)


def test_fresh_confirmation_still_passes(make_snapshot, incident, impact):
    snap = make_snapshot(lambda s: by_id(s["supplier_offerings"], "SO-02").update(
        confirmed_at="2026-10-18T09:00:00+08:00"))  # 2 days before as_of
    assert validate_plan(plan(rent("S-02", 20)), snap, impact, incident).status == FEASIBLE


def test_protect_high_priority_event_when_chairs_run_out(snapshot, incident, impact):
    inc, imp = _late(incident, impact, 40)
    result = plan_recovery(inc, snapshot, imp)
    protect = next(p for p in result.plans if p.actions[-1].action_type.value == "split_allocation")
    rent_part, split = protect.actions
    assert rent_part.parameters["supplier_id"] == "S-02" and rent_part.parameters["quantity"] == 25
    # 30 on hand + 25 rented = 55: wedding (high) gets all 50, seminar (medium) gets 5 of 20.
    assert split.parameters["allocations"] == [{"job_id": "J-01", "quantity": 50}, {"job_id": "J-02", "quantity": 5}]
    assert protect.validation.status == NEEDS
    assert failed_codes(protect.validation) == set()
    assert "J-02" in protect.validation.unresolved_assumptions[0]
    assert protect.estimated_cost.amount == Decimal("100.00")


def test_relying_on_fewer_unconfirmed_chairs_ranks_higher(snapshot, incident, impact):
    inc, imp = _late(incident, impact, 40)
    pending = [p for p in plan_recovery(inc, snapshot, imp).plans if p.validation.status == NEEDS]
    order = [[a.action_type.value for a in p.actions] for p in pending]
    rent_plus_rush = order.index(["source_alternative_supplier", "contact_supplier"])
    rush_all = order.index(["contact_supplier"])
    assert rent_plus_rush < rush_all  # Maju asked for 15, not 40
