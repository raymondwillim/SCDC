import copy
import json
from pathlib import Path

import pytest

from scdc.recovery.contracts import BusinessSnapshot, ImpactResult, NormalizedIncident

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "demo"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def seed_raw() -> dict:
    return load("seed.json")


@pytest.fixture
def snapshot(seed_raw) -> BusinessSnapshot:
    return BusinessSnapshot.model_validate(seed_raw)


@pytest.fixture
def incident() -> NormalizedIncident:
    return NormalizedIncident.model_validate(load("incident.json")["expected_normalized_incident"])


@pytest.fixture
def impact() -> ImpactResult:
    return ImpactResult.model_validate(load("expected_impact.json"))


@pytest.fixture
def expected_plans() -> dict:
    return load("expected_plans.json")


@pytest.fixture
def make_snapshot(seed_raw):
    """Build a modified copy of the seed: ``make_snapshot(lambda s: ...)``."""

    def _make(mutate) -> BusinessSnapshot:
        raw = copy.deepcopy(seed_raw)
        mutate(raw)
        return BusinessSnapshot.model_validate(raw)

    return _make


def by_id(items, id_):
    return next(i for i in items if i["id"] == id_)
