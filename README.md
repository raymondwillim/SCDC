# SCDC — Disruption Recovery Prototype

![tests](https://github.com/raymondwillim/SCDC/actions/workflows/tests.yml/badge.svg)

Hackathon prototype for small businesses: a supplier disruption comes in, the system finds which
commitments are affected, generates recovery plans, validates them with deterministic rules,
ranks them, and waits for human approval.

```
Supplier delay → Structured incident → Impact analysis → Recovery plans
→ Deterministic validation → Ranked comparison → Human approval → Prepared actions
```

## Team and status

| Module | Owner | Folder | Branch | State |
|---|---|---|---|---|
| Frontend | P1 | `frontend/` | `p1-frontend` | — |
| Backend / API / database | P2 | `backend/scdc/api/` | `p2-backend` | — |
| Impact analysis | P3 | `backend/scdc/impact/` | `p3-impact` | — |
| Recovery planning, validation, ranking | P4 | `backend/scdc/recovery/` | `p4-recovery` | ✅ done, 37 tests |
| AI extraction and explanation | P5 | `backend/scdc/ai/` | `p5-ai` | — |

## Run

```
cd scdc/scdc/backend
pip install pydantic pytest
python -m pytest -q              # run all tests
python -m scdc.recovery.demo     # print the demo recovery plans
```

Tests also run automatically on GitHub Actions on every push and pull request
(**Actions** tab → **tests** → **Run workflow** to run manually).

## Demo scenario

Seri Kenanga Events (fictional). Supplier Maju Rental delivers only 20 of 40 chairs on 21 Oct;
the rest arrive 23 Oct. Two events on 22 Oct need 70 chairs → **20 short**.

| Plan | Action | Result |
|---|---|---|
| RP-001 | Rent 20 chairs from Cahaya, arrives 21 Oct | ✅ feasible, rank 1, RM 80.00 |
| RP-002 | Order 20 from Delima (3-day lead time) | ❌ infeasible: arrives 23 Oct, too late |
| RP-003 | Ask Maju to deliver the remaining 20 by 21 Oct | ⚠️ needs confirmation from supplier |
| Edit | Change RP-001 from 20 chairs to 10 | ❌ infeasible: 10 chairs short |

## P4 — Recovery planning module

Generates recovery options from real business data only, simulates each one, and checks
9 rules: stock, supplier availability, lead time, quantity, deadline, double-booking,
date order, reschedulable commitment, unknown facts.

- **feasible**: all checks pass
- **infeasible**: at least one hard rule fails
- **needs_confirmation**: depends on a fact that is not confirmed yet

AI suggestions are optional and go through the same checks. The module works without AI.

How the backend uses it:

```python
from scdc.recovery import plan_recovery

result = plan_recovery(incident, snapshot, impact)
result.plans          # validated and ranked plans
result.explanations   # why each plan passed or failed
```

Full guide: `scdc/scdc/docs/RECOVERY.md`

## Docs

- `scdc/scdc/docs/CONTRACTS.md` — shared data contracts (all members)
- `scdc/scdc/docs/RECOVERY.md` — P4 integration guide
- `scdc/scdc/fixtures/demo/` — demo data, expected results, `recovery_output.json` (mock data for the frontend)

## How we work

1. Each member works on their own branch, e.g. `p4-recovery`.
2. Push changes: `git add .` → `git commit -m "..."` → `git push`
3. Open a pull request into `main`. Tests must pass ✅ before merging.