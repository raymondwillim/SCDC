"""Optional AI candidate ideas behind a replaceable adapter.

An AI suggester may only *propose* actions. This module:
- never raises if the model fails (the rules still produce plans),
- drops suggestions that do not match the action schema, with a warning,
- ignores any status, cost, rank or feasibility the model claims,
- keeps suggestions that reference unknown entities so the validator can
  reject them visibly (``infeasible``) instead of hiding the hallucination.

P5 implements a real ``CandidateSuggester`` (e.g. a Claude call that returns
JSON) and passes it to ``generate_plans(..., suggester=...)``.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .actions import ParameterError, parse_parameters
from .contracts import ActionType, BusinessSnapshot, ImpactResult, NormalizedIncident, PlanOrigin, TargetEntity


class SuggestedAction(BaseModel):
    model_config = ConfigDict(extra="ignore")  # e.g. a model-invented "status" is discarded
    action_type: ActionType
    target_entity: TargetEntity
    parameters: dict[str, Any]


class SuggestedPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")  # "feasible": true, "cost": ... are discarded
    summary: str = Field(min_length=1, max_length=300)
    actions: list[SuggestedAction] = Field(min_length=1, max_length=5)
    assumptions: list[str] = []


class CandidateSuggester(Protocol):
    name: str

    def suggest(
        self,
        incident: NormalizedIncident,
        snapshot: BusinessSnapshot,
        impact: ImpactResult,
        problems: list,
    ) -> list[dict[str, Any]]:
        """Return raw plan dicts shaped like ``SuggestedPlan``."""


class NullSuggester:
    """Default: no AI. The module is fully functional without a model."""

    name = "none"

    def suggest(self, incident, snapshot, impact, problems):
        return []


class StaticSuggester:
    """Returns fixed suggestions. For tests and demos without an API key."""

    def __init__(self, suggestions: list[dict[str, Any]], name: str = "static"):
        self.suggestions = suggestions
        self.name = name

    def suggest(self, incident, snapshot, impact, problems):
        return self.suggestions


def collect_suggestions(suggester, incident, snapshot, impact, problems):
    from .generator import Candidate  # local import avoids a cycle

    warnings: list[str] = []
    try:
        raw = suggester.suggest(incident, snapshot, impact, problems) or []
    except Exception as exc:  # model down, timeout, bad JSON ...
        return [], [f"AI suggester '{getattr(suggester, 'name', '?')}' failed ({exc}); rule-based plans only."]

    candidates = []
    for i, item in enumerate(raw):
        try:
            plan = SuggestedPlan.model_validate(item)
            for a in plan.actions:
                parse_parameters(a.action_type, a.parameters)
        except (ValidationError, ParameterError, TypeError) as exc:
            warnings.append(f"AI suggestion #{i + 1} rejected: does not match the action schema ({exc}).")
            continue
        candidates.append(
            Candidate(
                summary=plan.summary,
                actions=[(a.action_type, a.target_entity, a.parameters) for a in plan.actions],
                assumptions=plan.assumptions,
                created_by=PlanOrigin.ai_suggestion,
            )
        )
    return candidates, warnings
