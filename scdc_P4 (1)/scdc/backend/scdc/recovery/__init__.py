"""P4 — recovery planning: candidate generation, deterministic validation, ranking.

Pure functions only: contract objects in, contract objects out. No DB, HTTP or clock.
"""

from .ai_adapter import CandidateSuggester, NullSuggester, StaticSuggester
from .explain import PlanExplanation, explain_plan
from .generator import IdFactory, RecoveryInputError, generate_candidates, generate_plans, identify_problems
from .pipeline import RecoveryResult, StalePlanVersion, edit_plan, plan_recovery, revalidate
from .ranking import RankingWeights, rank_plans, rank_with_report, score_plans
from .validator import status_from_checks, validate_plan

__all__ = [
    "CandidateSuggester",
    "IdFactory",
    "NullSuggester",
    "PlanExplanation",
    "RankingWeights",
    "RecoveryInputError",
    "RecoveryResult",
    "StalePlanVersion",
    "StaticSuggester",
    "edit_plan",
    "explain_plan",
    "generate_candidates",
    "generate_plans",
    "identify_problems",
    "plan_recovery",
    "rank_plans",
    "rank_with_report",
    "revalidate",
    "score_plans",
    "status_from_checks",
    "validate_plan",
]
