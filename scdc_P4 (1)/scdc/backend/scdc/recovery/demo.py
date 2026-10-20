"""Run the recovery module on the demo fixtures.

    python -m scdc.recovery.demo            # readable summary
    python -m scdc.recovery.demo --json     # contract JSON (mock data for P1 / P5)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .contracts import BusinessSnapshot, ImpactResult, NormalizedIncident
from .pipeline import plan_recovery

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "demo"


def main(argv: list[str]) -> None:
    snapshot = BusinessSnapshot.model_validate_json((FIXTURES / "seed.json").read_text())
    incident = NormalizedIncident.model_validate(
        json.loads((FIXTURES / "incident.json").read_text())["expected_normalized_incident"]
    )
    impact = ImpactResult.model_validate_json((FIXTURES / "expected_impact.json").read_text())
    result = plan_recovery(incident, snapshot, impact)

    if "--json" in argv:
        print(json.dumps({
            "items": [p.to_contract() for p in result.plans],
            "explanations": [e.model_dump(mode="json") for e in result.explanations],
            "warnings": result.warnings,
            "next_steps": result.next_steps(),
        }, indent=2))
        return

    for p, e in zip(result.plans, result.explanations):
        cost = f"{p.estimated_cost.currency} {p.estimated_cost.amount:.2f}" if p.estimated_cost else "unknown"
        print(f"\n{p.id} v{p.version}  [{p.validation.status.value}]  rank={p.rank}  cost={cost}")
        print(f"  {p.summary}")
        for c in p.validation.checks:
            print(f"    {c.result.value:<10} {c.code.value:<24} {c.detail}")
        for item in e.required_confirmations:
            print(f"    needs confirmation: {item}")
        print(f"  {e.ranking_reason}")
    print("\nNext steps:", *result.next_steps(), sep="\n  ")


if __name__ == "__main__":
    main(sys.argv[1:])
