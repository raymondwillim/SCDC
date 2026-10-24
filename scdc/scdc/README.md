# SCDC — Disruption Recovery Prototype

![tests](../../actions/workflows/tests.yml/badge.svg)

Hackathon prototype: a supplier disruption comes in, the system finds which commitments are affected,
generates recovery plans, validates them with deterministic rules, ranks them, and waits for human approval.

## Status

| Module | Owner | Folder | State |
|---|---|---|---|
| Recovery planning, validation, ranking | P4 | `backend/scdc/recovery/` | done, 42 tests |
| Impact analysis | P3 | `backend/scdc/impact/` | — |
| Backend / API | P2 | `backend/scdc/api/` | — |
| AI adapter | P5 | `backend/scdc/ai/` | — |
| Frontend | P1 | `frontend/` | — |

## Run

```
cd backend
pip install pydantic pytest
python -m pytest -q              # tests
python -m scdc.recovery.demo     # demo scenario
```

## Docs

- `docs/CONTRACTS.md` — shared contracts
- `docs/RECOVERY.md` — P4 integration guide
- `fixtures/demo/` — demo scenario data and expected results
