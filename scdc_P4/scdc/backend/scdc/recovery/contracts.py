"""Local stand-in for the shared contract models (CONTRACTS.md v0.1, §3–§6).

P4 must be able to work before P2 publishes ``scdc.contracts``. Everything the
recovery module needs is defined here, field-for-field from CONTRACTS.md.
When ``scdc.contracts`` lands, delete the model definitions below and
re-export from there instead, e.g.::

    from scdc.contracts import BusinessSnapshot, ImpactResult, ...  # noqa: F401

Nothing else in ``scdc.recovery`` imports models from anywhere but this file,
so that swap is a one-file change.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0"


class _In(BaseModel):
    """Input records: tolerate extra keys (seed.json carries ``users`` etc.)."""

    model_config = ConfigDict(extra="ignore", use_enum_values=False)


class _Out(BaseModel):
    """Records this module produces: no surprise keys."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------- enums


class EntityType(str, Enum):
    supplier = "supplier"
    delivery = "delivery"
    resource = "resource"
    job = "job"
    commitment = "commitment"


class DependencyType(str, Enum):
    ships = "ships"
    supplies = "supplies"
    requires = "requires"
    fulfils = "fulfils"


class ImpactStatus(str, Enum):
    confirmed = "confirmed"
    potential_risk = "potential_risk"
    unresolved = "unresolved"


class ValidationStatus(str, Enum):
    feasible = "feasible"
    infeasible = "infeasible"
    needs_confirmation = "needs_confirmation"


class CheckResult(str, Enum):
    pass_ = "pass"
    fail = "fail"
    unresolved = "unresolved"


class CheckCode(str, Enum):
    RESOURCE_AVAILABLE = "RESOURCE_AVAILABLE"
    QUANTITY_SUFFICIENT = "QUANTITY_SUFFICIENT"
    SUPPLIER_AVAILABLE = "SUPPLIER_AVAILABLE"
    LEAD_TIME_OK = "LEAD_TIME_OK"
    DEADLINE_MET = "DEADLINE_MET"
    NO_DOUBLE_BOOKING = "NO_DOUBLE_BOOKING"
    DEPENDENCY_ORDER_OK = "DEPENDENCY_ORDER_OK"
    COMMITMENT_RESCHEDULABLE = "COMMITMENT_RESCHEDULABLE"
    FACTS_KNOWN = "FACTS_KNOWN"


class PlanStatus(str, Enum):
    proposed = "proposed"
    approved = "approved"
    rejected = "rejected"
    more_info_requested = "more_info_requested"
    superseded = "superseded"


class ActionType(str, Enum):
    use_spare_inventory = "use_spare_inventory"
    source_alternative_supplier = "source_alternative_supplier"
    reallocate_resource = "reallocate_resource"
    reschedule_job = "reschedule_job"
    split_allocation = "split_allocation"
    contact_supplier = "contact_supplier"


class ActionStatus(str, Enum):
    draft = "draft"
    approved = "approved"
    completed = "completed"
    failed = "failed"


class PlanOrigin(str, Enum):
    rules = "rules"
    ai_suggestion = "ai_suggestion"
    user_edit = "user_edit"


class DeliveryStatus(str, Enum):
    scheduled = "scheduled"
    delayed = "delayed"
    partially_delivered = "partially_delivered"
    delivered = "delivered"
    cancelled = "cancelled"


# --------------------------------------------------------------------------- shared


class Money(_In):
    amount: Decimal
    currency: str

    def to_contract(self) -> dict[str, str]:
        return {"amount": f"{self.amount:.2f}", "currency": self.currency}


# --------------------------------------------------------------------------- snapshot (§4)


class Supplier(_In):
    id: str
    name: str
    aliases: list[str] = []
    contact_reference: str | None = None
    reliability_notes: str | None = None


class SupplierOffering(_In):
    id: str
    supplier_id: str
    resource_id: str
    available_quantity: int | None = Field(default=None, ge=0)
    lead_time_days: int = Field(ge=0)
    unit_cost: Money | None = None
    availability_confirmed: bool = False
    confirmed_at: datetime | None = None


class Resource(_In):
    id: str
    name: str
    aliases: list[str] = []
    unit: str
    owned_quantity: int = Field(ge=0)
    on_hand_quantity: int = Field(ge=0)
    on_hand_verified: bool = False
    verified_at: datetime | None = None
    reusable: bool = True


class Delivery(_In):
    id: str
    supplier_id: str
    resource_id: str
    quantity: int = Field(ge=0)
    expected_date: date
    status: DeliveryStatus


class BusinessJob(_In):
    id: str
    name: str
    description: str | None = None
    customer_ref: str | None = None
    event_date: date
    deadline: date | None = None
    status: str
    priority: str


class Commitment(_In):
    id: str
    job_id: str
    commitment_type: str
    required_date: date
    reschedulable: bool
    status: str


class Dependency(_In):
    id: str
    source_type: EntityType
    source_id: str
    target_type: EntityType
    target_id: str
    dependency_type: DependencyType
    required_quantity: int | None = Field(default=None, ge=0)
    required_by: date | None = None


class Business(_In):
    id: str
    name: str
    timezone: str
    currency: str


class BusinessSnapshot(_In):
    schema_version: str = SCHEMA_VERSION
    as_of: datetime
    business: Business
    suppliers: list[Supplier] = []
    supplier_offerings: list[SupplierOffering] = []
    resources: list[Resource] = []
    deliveries: list[Delivery] = []
    jobs: list[BusinessJob] = []
    commitments: list[Commitment] = []
    dependencies: list[Dependency] = []
    missing_or_stale: list[Any] = []


# --------------------------------------------------------------------------- incident (§6.1)


class UnresolvedField(_In):
    field: str
    reason: str
    raw_value: Any = None
    candidates: list[Any] = []
    note: str | None = None


MATERIAL_INCIDENT_FIELDS = ("supplier_id", "resource_id", "quantity_affected", "new_arrival_date")


class NormalizedIncident(_In):
    schema_version: str = SCHEMA_VERSION
    id: str
    type: str
    source: str
    supplier_id: str | None = None
    resource_id: str | None = None
    delivery_id: str | None = None
    quantity_affected: int | None = Field(default=None, ge=0)
    original_arrival_date: date | None = None
    new_arrival_date: date | None = None
    description: str | None = None
    reported_at: datetime
    unresolved_fields: list[UnresolvedField] = []


# --------------------------------------------------------------------------- impact (§6.3)


class Evidence(_In):
    entity_id: str
    fact: str


class AffectedEntity(_In):
    entity_type: EntityType
    entity_id: str
    impact_status: ImpactStatus
    severity: int = Field(ge=0, le=10)
    reason: str
    dependency_path: list[str] = []
    evidence: list[Evidence] = []


class CheckedNotAffected(_In):
    entity_type: EntityType
    entity_id: str
    reason: str


class ResourceShortfall(_In):
    resource_id: str
    date: date
    required: int = Field(ge=0)
    available: int = Field(ge=0)
    shortfall: int = Field(ge=0)


class ImpactResult(_In):
    schema_version: str = SCHEMA_VERSION
    disruption_id: str
    affected_entities: list[AffectedEntity] = []
    checked_not_affected: list[CheckedNotAffected] = []
    resource_shortfalls: list[ResourceShortfall] = []
    unresolved_facts: list[Any] = []


# --------------------------------------------------------------------------- plans (§6.4, §6.5)


class TargetEntity(_Out):
    entity_type: EntityType
    entity_id: str


class RecoveryAction(_Out):
    id: str
    action_type: ActionType
    target_entity: TargetEntity
    # Kept as a plain dict so a malformed edit can still be *stored* and then
    # reported by the validator, instead of being impossible to represent.
    # Typed per action_type in ``scdc.recovery.actions``.
    parameters: dict[str, Any]
    status: ActionStatus = ActionStatus.draft


class ValidationCheck(_Out):
    code: CheckCode
    result: CheckResult
    detail: str
    evidence_ids: list[str] = []


class ValidationResult(_Out):
    plan_id: str
    plan_version: int
    status: ValidationStatus
    checks: list[ValidationCheck]
    unresolved_assumptions: list[str] = []
    validated_at: datetime


class RecoveryPlan(_Out):
    schema_version: str = SCHEMA_VERSION
    id: str
    disruption_id: str
    version: int = Field(default=1, ge=1)
    summary: str
    actions: list[RecoveryAction]
    assumptions: list[str] = []
    estimated_cost: Money | None = None
    validation: ValidationResult | None = None
    rank: int | None = None
    status: PlanStatus = PlanStatus.proposed
    created_by: PlanOrigin = PlanOrigin.rules

    @field_validator("actions")
    @classmethod
    def _at_least_one_action(cls, v: list[RecoveryAction]) -> list[RecoveryAction]:
        if not v:
            raise ValueError("a recovery plan needs at least one action")
        return v

    def to_contract(self) -> dict[str, Any]:
        """JSON-ready dict in the exact contract shape (money as string decimals)."""
        data = self.model_dump(mode="json")
        data["estimated_cost"] = self.estimated_cost.to_contract() if self.estimated_cost else None
        return data
