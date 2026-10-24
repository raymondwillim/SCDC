# P4 Recovery Planning — Integration Guide

Module: `backend/scdc/recovery/` · Owner: Person 4 · Follows CONTRACTS.md v0.1 (§3–§6)

```
cd backend
pip install -e ".[dev]"            # or: pip install pydantic pytest
python -m pytest tests/recovery -q # 42 tests
python -m scdc.recovery.demo       # demo scenario, readable
python -m scdc.recovery.demo --json > ../fixtures/demo/recovery_output.json   # mock data for P1/P5
```

## 1. Files

| File | Stage | What it does |
|---|---|---|
| `contracts.py` | — | Local copy of the contract models. **Only file to change when P2's `scdc.contracts` lands.** |
| `generator.py` | 1–3 | `identify_problems`, rule templates, normalisation to `RecoveryPlan`, `IdFactory` |
| `ai_adapter.py` | 2 | `CandidateSuggester` protocol, `NullSuggester` (default), `StaticSuggester` |
| `actions.py` | 3 | Typed `parameters` per `action_type` (§6.4) |
| `state.py` | 4 | Temporary business state + inventory maths |
| `validator.py` | 4 | `validate_plan`, deterministic, no AI |
| `costing.py` | 4 | `estimated_cost` from recorded unit prices only |
| `ranking.py` | 5 | `rank_plans`, `score_plans`, `RankingWeights` |
| `explain.py` | 6 | `explain_plan`, template-only `PlanExplanation` |
| `pipeline.py` | all | `plan_recovery` (whole chain), `edit_plan`, `revalidate` |

## 2. Entry points for P2

```python
from scdc.recovery import plan_recovery, edit_plan, validate_plan, rank_plans, IdFactory

# POST /disruptions/{id}/recovery-plans
result = plan_recovery(incident, snapshot, impact, ids=IdFactory(next_plan=n, next_action=m))
result.plans          # validated + ranked RecoveryPlan list -> {"items": [p.to_contract() ...]}
result.explanations   # PlanExplanation per plan (input for P5's explain())
result.warnings       # e.g. AI suggestion dropped; log as activity details
result.next_steps()   # what to show when nothing is feasible

# PUT /recovery-plans/{id}
old, new = edit_plan(plan, base_version=body.base_version, actions=body.actions,
                     assumptions=body.assumptions, snapshot=..., impact=..., incident=...)
# persist old (status=superseded) and new (version+1, revalidated). StalePlanVersion -> 409.

# POST /recovery-plans/{id}/revalidate
plan = revalidate(plan, snapshot, impact, incident)
```

The contract functions `generate_plans`, `validate_plan`, `rank_plans` are also exported on their own.

### Proposed contract changes (v0.2) — please review

1. **`validate_plan(plan, snapshot, impact, incident)`** — the validator needs the incident to know which
   part of the delivery is late and to model "expedite" requests. P2 already has it at that point.
2. **`rank_plans(plans, impact=None, commitment_jobs=None, weights=...)`** — the impact result is needed to
   count protected commitments. Without it, ranking still works but protection/deadline factors are 1.0.
3. **`contact_supplier` parameters**: optional `resource_id` and `quantity` (for asking a non-disrupted
   supplier for stock). Not used by the demo; backward compatible.
4. Rank is only set for `feasible` plans. `needs_confirmation` plans get a provisional score (in
   `PlanScore`, not in the plan) and keep `rank: null`, as in `expected_plans.json`.

## 3. Input requirements

**NormalizedIncident** (§6.1): `supplier_id`, `resource_id`, `quantity_affected`, `new_arrival_date` must
be set and not in `unresolved_fields`, or `RecoveryInputError` is raised (maps to 409
`INVALID_STATE_TRANSITION`). `delivery_id` is strongly recommended. Without it only an unambiguous
supplier + resource + original-date match is accepted.

**BusinessSnapshot** (§4), the **pre-disruption** state. P4 applies the incident itself:
the delivery's `quantity − quantity_affected` arrives on `expected_date`, and `quantity_affected` arrives on
`new_arrival_date`. Fields used:

| Entity | Fields read |
|---|---|
| Resource | `on_hand_quantity`, `on_hand_verified`, `owned_quantity` (spare = owned − on hand), `reusable`, `unit` |
| Delivery | `quantity`, `expected_date`, `status` (scheduled / delayed / partially_delivered count as inbound) |
| SupplierOffering | `available_quantity`, `availability_confirmed`, `lead_time_days`, `unit_cost` |
| Dependency (`requires`) | `required_quantity`, `required_by` (falls back to job `event_date`) |
| Commitment | `job_id`, `reschedulable` |
| `as_of`, `business.timezone`, `business.currency` | "today", money currency |
| `missing_or_stale[]` | Items with an `entity_id` the plan uses, or items with no `entity_id`, make the plan `needs_confirmation` |

**ImpactResult** (§6.3): `resource_shortfalls` decides what to recover. `affected_entities` (jobs,
commitments) feeds ranking. `unresolved_facts` makes every plan `needs_confirmation`.
The validator recomputes the baseline. If its result differs from the impact result's `available`, the
plan is flagged `needs_confirmation`, so a disagreement between P3 and P4 shows up instead of passing silently.

## 4. Inventory rule

Per resource and day `d` (CONTRACTS §4):

```
pool(d)   = on_hand + arrivals with date < d   (− units consumed earlier, consumables only)
demand(d) = sum of required quantities of jobs on d
covered   ⇔ pool(d) ≥ demand(d)
```

An arrival **on** the event day does not count. Rented or bought units a plan adds count only on the
shortfall days, because return terms are not recorded.

## 5. Candidate plans

Templates (in output order). Each one only uses recorded entities and numbers:

1. **use_spare_inventory**: when owned − on hand ≥ shortfall. Always `needs_confirmation` (spare stock is unverified).
2. **source_alternative_supplier**: one plan per offer from a supplier other than the disrupted one,
   quantity = shortfall, order today, arrival = today + lead time. Offers recorded with 0 units are skipped.
   Slow or unconfirmed offers are still generated, so the owner can see why they were rejected.
3. **combination**: spare + confirmed in-time offers, cheapest first. Only when no single source covers the shortfall.
3b. **partial supply + expedite**: when even combined in-time stock is not enough, take all of it and ask the
   disrupted supplier to rush only the remaining units (e.g. 25 from Cahaya + 15 from Maju). Always `needs_confirmation`.
4. **reschedule_job**: an affected job whose commitments are all reschedulable and whose quantity covers
   the shortfall, moved to the first day within 7 days that has room.
5. **contact_supplier** (expedite): ask the disrupted supplier to deliver the late units the day before the need date.
6. **protect priority**: when confirmed in-time supply cannot cover every job, use all of it and give jobs their full
   quantity in priority order (critical > high > medium > low). Lower-priority jobs get what is left
   (e.g. wedding 50/50, seminar 5/20). Always `needs_confirmation`, because the customer must accept less.

`reallocate_resource` is **not** generated by rules (and `split_allocation` only by template 6), because no minimum quantity or
partial-fulfilment permission is recorded. They are fully validated when they come from AI or a user
edit. A job left below its requirement gives `QUANTITY_SUFFICIENT: unresolved` (customer must agree).

Plan schema: CONTRACTS §6.4 exactly. `estimated_cost` = additional purchase cost: quantity × recorded
`unit_cost` for supplier orders, `0.00` for actions that buy nothing, `null` if any part is unknown
(e.g. expediting).

## 6. Validation

Status rule: any `fail` → `infeasible`. Otherwise any `unresolved` → `needs_confirmation`. Otherwise `feasible`.

| Check | Fails when | Unresolved when |
|---|---|---|
| RESOURCE_AVAILABLE | unknown resource/job, spare stock not recorded, moving more than allocated | spare stock exists but is unverified |
| SUPPLIER_AVAILABLE | unknown supplier, plan needs more than offered (summed across actions) | not confirmed, confirmation older than 72 h (`MAX_CONFIRMATION_AGE`), quantity unknown, no recorded offer, any contact request |
| LEAD_TIME_OK | order + lead time is not before need date, or plan claims a faster arrival | no lead time recorded |
| QUANTITY_SUFFICIENT | units after plan < required on a shortfall day (timing ignored) | a job gets less than its requirement |
| DEADLINE_MET | enough units, but they arrive on/after the day needed | — |
| NO_DOUBLE_BOOKING | another day that was covered (or less short) becomes short | — |
| DEPENDENCY_ORDER_OK | order / new / requested date in the past, arrival before order | — |
| COMMITMENT_RESCHEDULABLE | moving a job with a non-reschedulable commitment | job has no commitment recorded |
| FACTS_KNOWN | action parameters malformed | any plan assumption, unverified on-hand count, stale data, impact mismatch |

Simulated supply is capped at what the records say exists: ordering 40 from a supplier with 25, or using
20 spare when 10 exist, counts 25 / 10 in the quantity check, so the reported shortfall is the real one.

`QUANTITY_SUFFICIENT` and `DEADLINE_MET` are separate so the owner sees "too few" (edit 20 → 10) apart
from "too late" (Delima, 3-day lead time).
Expedite requests are simulated *as if granted*, so a request that would not help even if the supplier agrees is `infeasible`.

## 7. Ranking

Only feasible plans get `rank` (1 = best). Score = weighted mean of factors in [0, 1]:

| Factor | Default weight | Definition |
|---|---|---|
| protection | 0.35 | affected commitments kept on their date with full quantity / affected commitments |
| deadlines | 0.25 | affected jobs kept on their date with full quantity / affected jobs |
| cost | 0.20 | linear between cheapest (1) and dearest (0) known cost; **unknown = 0** |
| certainty | 0.15 | mean of 1 − min(1, unresolved / 3) and 1 − share of the shortfall resting on unconfirmed units (rush requests, unverified spare) |
| simplicity | 0.05 | 1 / number of actions |

Ties: lower cost, fewer actions, plan id. Configure via `RankingWeights(...)`. Every plan gets a
`ranking_reason` showing the arithmetic.

## 8. AI suggestions (P5)

Implement `suggest(incident, snapshot, impact, problems) -> list[dict]` returning
`{"summary", "actions": [{"action_type", "target_entity", "parameters"}], "assumptions"}` and pass
`suggester=` to `plan_recovery`. The module then:
- ignores any status, cost or rank the model returns,
- drops schema-invalid suggestions with a warning,
- keeps suggestions with invented entities, which then fail validation visibly,
- falls back to rules only if the suggester raises, with a warning and no error.

## 9. Errors

| Situation | Behaviour | Suggested HTTP |
|---|---|---|
| Incident has unresolved material fields | `RecoveryInputError` | 409 `INVALID_STATE_TRANSITION` |
| Edit on old version | `StalePlanVersion` | 409 `STALE_PLAN_VERSION` |
| Malformed action parameters in an edit | plan stored, `infeasible` with detail | 200 |
| `rank_plans` on unvalidated plans | `ValueError` | 500 (programming error) |
| Approving `infeasible` | not P4's endpoint, but `plan.validation.status` is what P2 must check | 409 `PLAN_INFEASIBLE` |

## 10. Swapping mocks for real modules

1. When P2 publishes `scdc.contracts`, replace the model definitions in `recovery/contracts.py` with
   re-exports. Keep `Money.to_contract`/`RecoveryPlan.to_contract` or move them to P2's models.
2. Replace `expected_impact.json` with `analyze_impact(incident, snapshot)` from P3. The baseline
   cross-check (§3) will flag any disagreement.
3. `IdFactory(next_plan, next_action)` gets the next free IDs from the DB.
4. The tests keep using the fixtures, so they stay independent of P2/P3.
