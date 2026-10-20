"""Typed ``parameters`` for each ``ActionType`` (CONTRACTS.md §6.4).

Actions are stored with ``parameters`` as a plain dict. The validator calls
:func:`parse_parameters` and turns a parse error into a failed check, so a
malformed user edit or AI suggestion is reported as infeasible rather than
crashing the pipeline or being silently dropped.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import ActionType


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UseSpareInventory(_Params):
    resource_id: str
    quantity: int = Field(ge=1)
    location: str | None = None


class SourceAlternativeSupplier(_Params):
    supplier_id: str
    resource_id: str
    quantity: int = Field(ge=1)
    order_date: date
    expected_arrival_date: date


class ReallocateResource(_Params):
    resource_id: str
    quantity: int = Field(ge=1)
    from_job_id: str
    to_job_id: str


class RescheduleJob(_Params):
    job_id: str
    new_date: date


class Allocation(_Params):
    job_id: str
    quantity: int = Field(ge=0)


class SplitAllocation(_Params):
    resource_id: str
    allocations: list[Allocation] = Field(min_length=1)


class ContactSupplier(_Params):
    supplier_id: str
    request: str = Field(min_length=1)
    requested_date: date | None = None
    # Optional extensions (proposed for contract v0.2). When omitted and the
    # supplier is the disrupted one, the request is read as "expedite the
    # delayed quantity of the incident's delivery to requested_date".
    resource_id: str | None = None
    quantity: int | None = Field(default=None, ge=1)


PARAMETER_MODELS: dict[ActionType, type[_Params]] = {
    ActionType.use_spare_inventory: UseSpareInventory,
    ActionType.source_alternative_supplier: SourceAlternativeSupplier,
    ActionType.reallocate_resource: ReallocateResource,
    ActionType.reschedule_job: RescheduleJob,
    ActionType.split_allocation: SplitAllocation,
    ActionType.contact_supplier: ContactSupplier,
}


class ParameterError(ValueError):
    pass


def parse_parameters(action_type: ActionType, parameters: dict) -> _Params:
    model = PARAMETER_MODELS[action_type]
    try:
        return model.model_validate(parameters)
    except ValidationError as exc:
        issues = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'parameters'}: {err['msg']}" for err in exc.errors()
        )
        raise ParameterError(issues) from exc
