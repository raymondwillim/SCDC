# SCDC Shared Contracts — v0.1 (DRAFT for Phase 1 review)

Status: **Proposal**. Owner: Person 2 (consolidates). Reviewers: all five members.
Every section marked **[DECIDE]** needs an explicit team answer before Phase 2 starts.
Once signed off, bump to `v1.0` and treat any change as a versioned contract change (announce + update fixtures).

Companion fixtures: [`fixtures/demo/`](../fixtures/demo/) — the demo scenario as JSON, with expected outputs.

---

## 0. Phase 1 checklist

| # | Item | Section | Owner | Agreed? |
|---|---|---|---|---|
| 1 | Tech stack and repo layout | §1 | All | ☐ |
| 2 | Conventions (IDs, dates, money, JSON style) | §2 | P2 | ☐ |
| 3 | Status values and enums | §3 | All | ☐ |
| 4 | Entities and database fields | §4 | P2 | ☐ |
| 5 | Module interfaces (pure functions) | §5 | P2–P5 | ☐ |
| 6 | Result schemas (incident, impact, plan, validation, explanation) | §6 | P3, P4, P5 | ☐ |
| 7 | API endpoints | §7 | P1, P2 | ☐ |
| 8 | Error format | §8 | P1, P2 | ☐ |
| 9 | Demo scenario and expected results | §9 | All | ☐ |
| 10 | Open decisions | §10 | All | ☐ |

---

## 1. Stack and repository layout **[DECIDE]**

Proposal, following the README's suggestion:

- **Backend and modules:** Python 3.11+, FastAPI, Pydantic v2, SQLite (via SQLAlchemy), pytest.
- **Frontend:** React + TypeScript + Vite.
- **One repository, one branch per person**, small PRs into `main`.

```
scdc/
  docs/CONTRACTS.md            # this file (P2)
  fixtures/demo/               # shared scenario JSON (everyone reads, P2 merges changes)
  backend/
    scdc/
      contracts/               # Pydantic models generated from §3–§6 (P2) — the ONLY shared types
      api/                     # FastAPI routes, DB, seed, auth, activity log (P2)
      impact/                  # impact analysis (P3)
      recovery/                # plan generation, validation, ranking (P4)
      ai/                      # adapter, extraction, explanation, drafts, eval (P5)
    tests/
  frontend/                    # P1
```

Rule: `impact`, `recovery`, and `ai` **never touch the database or HTTP**. They take contract objects in and return contract objects out (§5). That is what lets P3–P5 work on fixtures alone.

---

## 2. Conventions

| Topic | Rule |
|---|---|
| JSON keys | `snake_case` |
| IDs | String, prefix + zero-padded number: `S-01`, `R-01`, `J-01`, `CM-01`, `DL-01`, `DEP-01`, `SO-01`, `U-01`, `D-001`, `RP-001`, `RA-001`, `AP-001`, `LOG-0001`, `DM-001`. Seed data uses 2 digits; runtime records use 3–4. IDs are opaque; never parse them. |
| Dates | `YYYY-MM-DD` (calendar date, business timezone) |
| Timestamps | ISO 8601 with offset, e.g. `2026-10-20T10:15:00+08:00`. Stored in UTC, displayed in `Asia/Kuala_Lumpur`. |
| Quantities | Non-negative integers |
| Money | `{"amount": "80.00", "currency": "MYR"}` (string decimal, so no float rounding). `null` = unknown, never `0`. **[DECIDE]** |
| Unknown values | `null` plus an entry in that object's `unresolved_*` list. Never invent a default. |
| Enums | Lowercase `snake_case` strings (§3). Reject unknown values. |
| Versioning | Every top-level result carries `"schema_version": "1.0"`. |
| Provenance | Any text produced by a model carries `generated_by` (`"template"` or `"model:<name>"`). |

---

## 3. Enums

```text
DisruptionType      supplier_delay | supplier_shortfall | resource_unavailable | staff_absence
                    (MVP demo uses supplier_delay only)
InputSource         form | message
DisruptionStatus    received | needs_clarification | validated | analyzed | plans_ready | decided | closed
EntityType          supplier | delivery | resource | job | commitment
DependencyType      ships      (supplier -> delivery)
                    supplies   (delivery -> resource)
                    requires   (resource -> job, carries required_quantity + required_by)
                    fulfils    (job -> commitment)
ImpactStatus        confirmed | potential_risk | unresolved
ValidationStatus    feasible | infeasible | needs_confirmation
CheckResult         pass | fail | unresolved
CheckCode           RESOURCE_AVAILABLE | QUANTITY_SUFFICIENT | SUPPLIER_AVAILABLE | LEAD_TIME_OK |
                    DEADLINE_MET | NO_DOUBLE_BOOKING | DEPENDENCY_ORDER_OK | COMMITMENT_RESCHEDULABLE |
                    FACTS_KNOWN
PlanStatus          proposed | approved | rejected | more_info_requested | superseded
ActionType          use_spare_inventory | source_alternative_supplier | reallocate_resource |
                    reschedule_job | split_allocation | contact_supplier
ActionStatus        draft | approved | completed | failed
Decision            approved | rejected | more_info_requested
                    (an "edit" is not a decision: it creates a new plan version that must be revalidated)
Priority            low | medium | high | critical
Role                owner | staff | viewer        (only owner may approve/reject)
UnresolvedReason    missing | ambiguous | unmatched | conflicting | unverified
RecipientType       supplier | customer
ActivityType        disruption_reported | disruption_extracted | disruption_validated | analysis_completed |
                    plans_generated | plan_edited | plan_revalidated | plan_approved | plan_rejected |
                    info_requested | approval_denied | draft_created | draft_approved | action_completed
```

Rules for `ValidationStatus` (P4 owns, nobody else may set it):
- any check `fail` → `infeasible`
- else any check `unresolved` → `needs_confirmation`
- else → `feasible`

---

## 4. Entities (database + `BusinessSnapshot`)

Changes from the README's entity list, all needed by the demo:
- **Added `Delivery`**: a supplier delay is a delay *of a specific inbound order*. Without it there is nothing to compare the new date against.
- **Added `SupplierOffering`**: alternative suppliers need quantity, lead time, cost, and whether availability is confirmed (the plan explicitly says "existing in the DB" ≠ "available").
- **Added `aliases` on Supplier and Resource**: P5 resolves names from messages ("Maju", "kerusi lipat") against these.
- **Added `User`** for permission checks.

| Entity | Fields |
|---|---|
| **Supplier** | `id`, `name`, `aliases[]`, `contact_reference`, `reliability_notes` |
| **SupplierOffering** | `id`, `supplier_id`, `resource_id`, `available_quantity` (int\|null), `lead_time_days` (int), `unit_cost` (Money\|null), `availability_confirmed` (bool), `confirmed_at` (ts\|null) |
| **Resource** | `id`, `name`, `aliases[]`, `unit`, `owned_quantity`, `on_hand_quantity`, `on_hand_verified` (bool), `verified_at`, `reusable` (bool — returned after each job) |
| **Delivery** | `id`, `supplier_id`, `resource_id`, `quantity`, `expected_date`, `status` (`scheduled`\|`delayed`\|`partially_delivered`\|`delivered`\|`cancelled`) |
| **BusinessJob** | `id`, `name`, `description`, `customer_ref`, `event_date`, `deadline`, `status`, `priority` |
| **Commitment** | `id`, `job_id`, `commitment_type`, `required_date`, `reschedulable` (bool), `status` |
| **Dependency** | `id`, `source_type`, `source_id`, `target_type`, `target_id`, `dependency_type`, `required_quantity` (int\|null), `required_by` (date\|null) |
| **Disruption** | `id`, `type`, `source`, `raw_message`, `description`, `reported_at`, `reported_by`, `status`, `incident` (NormalizedIncident JSON), `severity` |
| **RecoveryPlan** | `id`, `disruption_id`, `version`, `summary`, `actions[]`, `assumptions[]`, `estimated_cost`, `validation` (ValidationResult JSON), `rank`, `status`, `created_by` (`rules`\|`ai_suggestion`\|`user_edit`) |
| **RecoveryAction** | `id`, `plan_id`, `action_type`, `target_entity`, `parameters` (JSON, §6.4), `status` |
| **Approval** | `id`, `plan_id`, `plan_version`, `reviewer_id`, `decision`, `comments`, `decided_at` |
| **ActivityLog** | `id`, `disruption_id`, `actor_id` (`U-xx` or `system`), `activity_type`, `details` (JSON), `created_at` |
| **DraftMessage** | `id`, `plan_id`, `recipient_type`, `recipient_id`, `body`, `placeholders[]`, `status`, `generated_by` |
| **User** | `id`, `name`, `role` |

**BusinessSnapshot** = the read-only bundle P2 builds for one disruption and hands to P3/P4/P5:

```json
{
  "schema_version": "1.0",
  "as_of": "2026-10-20T10:15:00+08:00",
  "business": {"id": "B-01", "name": "...", "timezone": "Asia/Kuala_Lumpur", "currency": "MYR"},
  "suppliers": [], "supplier_offerings": [], "resources": [], "deliveries": [],
  "jobs": [], "commitments": [], "dependencies": [],
  "missing_or_stale": []
}
```

Inventory rule for the MVP: a reusable resource is occupied only on its job's `event_date` and returns the next day. Available on day *d* = `on_hand_quantity` + deliveries with `expected_date < d` − requirements of other jobs on day *d*.

---

## 5. Module interfaces

All pure functions. Input and output are contract models; no DB, no HTTP, no global state.

```python
# P2 — backend
build_snapshot(incident: NormalizedIncident) -> BusinessSnapshot

# P3 — impact
analyze_impact(incident: NormalizedIncident, snapshot: BusinessSnapshot) -> ImpactResult

# P4 — recovery
generate_plans(incident, snapshot, impact: ImpactResult) -> list[RecoveryPlan]       # validation not yet filled
validate_plan(plan: RecoveryPlan, snapshot, impact) -> ValidationResult              # deterministic, re-run after every edit
rank_plans(plans: list[RecoveryPlan]) -> list[RecoveryPlan]                         # sets rank; infeasible get rank null

# P5 — AI (each has a deterministic fallback; never raises on model failure)
extract_incident(message: str, snapshot: BusinessSnapshot, reported_at: datetime) -> ExtractionResult
explain(incident, impact, plans: list[RecoveryPlan]) -> Explanation
draft_messages(plan: RecoveryPlan, incident, snapshot) -> list[DraftMessage]        # only for approved plans
```

Orchestration (P2): `extract (optional) → human confirms form → validate → build_snapshot → analyze_impact → generate_plans → validate_plan (each) → rank_plans → explain → human decision → draft_messages`.

---

## 6. Result schemas

### 6.1 NormalizedIncident

```json
{
  "schema_version": "1.0",
  "id": "D-001",
  "type": "supplier_delay",
  "source": "message",
  "supplier_id": "S-01",
  "resource_id": "R-01",
  "delivery_id": "DL-01",
  "quantity_affected": 20,
  "original_arrival_date": "2026-10-21",
  "new_arrival_date": "2026-10-23",
  "description": "S-01 will deliver only 20 of 40 chairs on 21 Oct; remaining 20 on 23 Oct.",
  "reported_at": "2026-10-20T10:15:00+08:00",
  "unresolved_fields": []
}
```

`unresolved_fields[]` item: `{"field": "quantity_affected", "reason": "missing", "raw_value": null, "candidates": [], "note": "..."}`.
An incident with any unresolved **material** field (`supplier_id`, `resource_id`, `quantity_affected`, `new_arrival_date`) gets status `needs_clarification` and is not analyzed.

### 6.2 ExtractionResult (P5 → form prefill, never stored as validated)

```json
{
  "schema_version": "1.0",
  "draft_incident": { "...NormalizedIncident without id..." },
  "field_evidence": {"new_arrival_date": "\"the remaining 20 chairs on Friday\""},
  "unresolved_fields": [],
  "needs_clarification": false,
  "clarification_questions": [],
  "generated_by": "model:<name>" ,
  "fallback_used": false,
  "warnings": []
}
```

### 6.3 ImpactResult (P3)

```json
{
  "schema_version": "1.0",
  "disruption_id": "D-001",
  "affected_entities": [
    {
      "entity_type": "job",
      "entity_id": "J-01",
      "impact_status": "confirmed",
      "severity": 8,
      "reason": "Chair pool short by 20 on 2026-10-22; J-01 requires 50.",
      "dependency_path": ["S-01", "DL-01", "R-01", "J-01"],
      "evidence": [{"entity_id": "DEP-03", "fact": "required_quantity=50, required_by=2026-10-22"}]
    }
  ],
  "checked_not_affected": [
    {"entity_type": "job", "entity_id": "J-03", "reason": "Needed 2026-10-25; delayed chairs arrive 2026-10-23."}
  ],
  "resource_shortfalls": [{"resource_id": "R-01", "date": "2026-10-22", "required": 70, "available": 50, "shortfall": 20}],
  "unresolved_facts": []
}
```

`severity`: integer 0–10. Formula and weights live in P3's config and are documented there; tests assert `impact_status` and `dependency_path`, not exact severity.

### 6.4 RecoveryPlan + actions (P4)

```json
{
  "schema_version": "1.0",
  "id": "RP-001",
  "disruption_id": "D-001",
  "version": 1,
  "summary": "Rent 20 chairs from Cahaya Event Rentals for delivery on 21 Oct.",
  "actions": [
    {
      "id": "RA-001",
      "action_type": "source_alternative_supplier",
      "target_entity": {"entity_type": "resource", "entity_id": "R-01"},
      "parameters": {"supplier_id": "S-02", "resource_id": "R-01", "quantity": 20,
                     "order_date": "2026-10-20", "expected_arrival_date": "2026-10-21"},
      "status": "draft"
    }
  ],
  "assumptions": [],
  "estimated_cost": {"amount": "80.00", "currency": "MYR"},
  "validation": { "...ValidationResult..." },
  "rank": 1,
  "status": "proposed",
  "created_by": "rules"
}
```

`parameters` per `action_type`:

| action_type | parameters |
|---|---|
| `use_spare_inventory` | `resource_id`, `quantity`, `location?` |
| `source_alternative_supplier` | `supplier_id`, `resource_id`, `quantity`, `order_date`, `expected_arrival_date` |
| `reallocate_resource` | `resource_id`, `quantity`, `from_job_id`, `to_job_id` |
| `reschedule_job` | `job_id`, `new_date` |
| `split_allocation` | `resource_id`, `allocations: [{job_id, quantity}]` |
| `contact_supplier` | `supplier_id`, `request` (text), `requested_date?` |

### 6.5 ValidationResult (P4, deterministic only)

```json
{
  "plan_id": "RP-002",
  "plan_version": 1,
  "status": "infeasible",
  "checks": [
    {"code": "SUPPLIER_AVAILABLE", "result": "pass", "detail": "S-03 has 60 confirmed.", "evidence_ids": ["SO-03"]},
    {"code": "DEADLINE_MET", "result": "fail", "detail": "Earliest arrival 2026-10-23 is after 2026-10-22.", "evidence_ids": ["SO-03", "DEP-03", "DEP-04"]}
  ],
  "unresolved_assumptions": [],
  "validated_at": "2026-10-20T10:16:00+08:00"
}
```

### 6.6 Explanation (P5; UI must label it AI-generated when `generated_by` starts with `model:`)

```json
{
  "schema_version": "1.0",
  "disruption_id": "D-001",
  "summary": "...",
  "impact_explanations": [{"entity_id": "J-01", "text": "..."}],
  "plan_comparisons": [
    {"plan_id": "RP-001", "validation_status": "feasible", "benefits": ["..."], "limitations": ["..."]}
  ],
  "missing_confirmations": ["..."],
  "next_steps": ["..."],
  "generated_by": "template",
  "fallback_used": true,
  "unsupported_claims_removed": 0
}
```

`validation_status` here is **copied** from the ValidationResult, never generated.

### 6.7 DraftMessage (P5)

```json
{
  "id": "DM-001", "plan_id": "RP-001", "recipient_type": "supplier", "recipient_id": "S-02",
  "body": "Hi Cahaya Event Rentals, we would like to rent 20 folding chairs for delivery on 21 Oct 2026 to [DELIVERY_ADDRESS]...",
  "placeholders": ["DELIVERY_ADDRESS"],
  "status": "draft", "generated_by": "template"
}
```

Drafts are never sent. `status` moves to `approved` only via a separate human action.

---

## 7. API (base path `/api/v1`)

Auth for the MVP: header `X-User-Id: U-01`. Backend looks up the role; no passwords. **[DECIDE]**

| Method & path | Request | Response | Notes |
|---|---|---|---|
| GET `/jobs`, `/suppliers`, `/resources`, `/deliveries`, `/dependencies` | — | `{"items": [...]}` | `/suppliers` embeds `offerings` |
| GET `/dashboard` | — | active disruptions, affected commitments count, pending approvals | for P1 home screen |
| POST `/disruptions/extract` | `{"message": "..."}` | `ExtractionResult` | Does **not** create a disruption; prefills the form |
| POST `/disruptions` | `DisruptionCreate` (incident fields + `source`, optional `raw_message`, optional `client_request_id`) | `201 {disruption, incident, validation_errors}` | 409 `DUPLICATE_DISRUPTION` if same delivery + same new date already open |
| GET `/disruptions`, `/disruptions/{id}` | — | Disruption(s) | |
| POST `/disruptions/{id}/analyze` | — | `ImpactResult` | 409 if status is `needs_clarification` |
| GET `/disruptions/{id}/impact` | — | `ImpactResult` | |
| POST `/disruptions/{id}/recovery-plans` | — | `{"items": [RecoveryPlan]}` (validated + ranked) | |
| GET `/disruptions/{id}/recovery-plans` | — | `{"items": [RecoveryPlan]}` | |
| GET `/disruptions/{id}/explanation` | — | `Explanation` | Always 200; falls back to templates |
| GET `/recovery-plans/{id}` | — | `RecoveryPlan` | |
| PUT `/recovery-plans/{id}` | `{"base_version": 1, "actions": [...], "assumptions": [...]}` | `RecoveryPlan` (version+1, revalidated) | Edit = new version; old version `superseded` |
| POST `/recovery-plans/{id}/revalidate` | — | `RecoveryPlan` | |
| POST `/recovery-plans/{id}/approve` | `{"plan_version": 2, "comments": ""}` | `Approval` | 403 non-owner; 409 stale version or infeasible |
| POST `/recovery-plans/{id}/reject` | `{"plan_version": 1, "comments": "..."}` | `Approval` | |
| POST `/recovery-plans/{id}/request-info` | `{"plan_version": 1, "comments": "..."}` | `Approval` | |
| POST `/recovery-plans/{id}/drafts` | — | `{"items": [DraftMessage]}` | Only for `approved` plans |
| POST `/drafts/{id}/approve` | — | `DraftMessage` | Marks approved; still not sent |
| GET `/disruptions/{id}/activity-log` | — | `{"items": [ActivityLog]}` | Newest last |

---

## 8. Error format

All non-2xx responses:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Human-readable summary.",
    "details": [{"field": "quantity_affected", "issue": "must be >= 1"}],
    "request_id": "req-7f3a"
  }
}
```

| HTTP | code | When |
|---|---|---|
| 400 | `BAD_REQUEST` | Malformed JSON |
| 403 | `FORBIDDEN` | Role not allowed (logged as `approval_denied`) |
| 404 | `NOT_FOUND` | Unknown ID |
| 409 | `DUPLICATE_DISRUPTION` | Same incident already open (`details` includes existing id) |
| 409 | `STALE_PLAN_VERSION` | Approving or editing an old version |
| 409 | `PLAN_INFEASIBLE` | Approving an infeasible plan |
| 409 | `INVALID_STATE_TRANSITION` | e.g. analyzing a `needs_clarification` disruption |
| 422 | `VALIDATION_ERROR` | Schema or business-rule failure on input |
| 500 | `INTERNAL_ERROR` | Anything else |

AI unavailability is **not** an API error: P5 modules fall back and set `fallback_used: true`.

---

## 9. Demo scenario (fixtures in `fixtures/demo/`)

**Business:** Seri Kenanga Events (fictional event planner, Kuala Lumpur). "Today" = Tue 2026-10-20.

| Fact | Value |
|---|---|
| Chairs owned and on hand (R-01) | 30, verified |
| Inbound order DL-01 from Maju Rental (S-01) | 40 chairs, expected Wed 21 Oct |
| J-01 wedding reception, Thu 22 Oct | needs 50 chairs (not reschedulable) |
| J-02 corporate seminar, Thu 22 Oct | needs 20 chairs (not reschedulable) |
| J-03 birthday party, Sun 25 Oct | needs 15 chairs |
| Alternative S-02 Cahaya Event Rentals | 25 chairs, 1-day lead, RM 4.00 each, availability **confirmed** |
| Alternative S-03 Delima Furniture | 60 chairs, 3-day lead, RM 2.80 each, availability confirmed |

**Disruption (WhatsApp from S-01, 10:15):** only 20 chairs arrive Wed; the remaining 20 come Friday 23 Oct.

**Expected results:**
- Shortfall on 22 Oct: required 70, available 30 + 20 = 50 → **short 20**.
- J-01, J-02 (and CM-01, CM-02) → `confirmed`. J-03 → checked, not affected.
- RP-001 rent 20 from S-02 → **feasible**, RM 80.00, rank 1.
- RP-002 order 20 from S-03 → **infeasible** (`DEADLINE_MET` fails: arrives 23 Oct).
- RP-003 ask S-01 to expedite remaining 20 to 21 Oct → **needs_confirmation**.
- Edit demo: change RP-001 quantity 20 → 10 → revalidates to **infeasible** (`QUANTITY_SUFFICIENT` fails, short 10).
- Permission demo: U-02 (staff) tries to approve → 403.
- Duplicate demo: same message submitted twice → 409.

---

## 10. Open decisions **[DECIDE]**

1. **Stack and layout** (§1): confirm Python/FastAPI/SQLite + React/TS.
2. **Can a `needs_confirmation` plan be approved?** Proposal: yes, but approval generates a mandatory `contact_supplier` follow-up, and the plan's outstanding confirmations stay visible. `infeasible` can never be approved.
3. **Shared-pool impact:** when two jobs share a short resource and no allocation priority exists, proposal is to mark both `confirmed` with the reason "shared pool short by N". Alternative: mark them `potential_risk`.
4. **Money format** (§2): string decimal vs integer sen.
5. **Auth:** `X-User-Id` header with seeded users is enough for the MVP?
6. **AI provider:** P5 proposes one optional provider behind the adapter; the demo must run fully with no API key.
7. **Project name** (README says TBD).
