"""Simulated inventory state: the temporary copy of the business that plans are applied to.

Inventory rule (CONTRACTS.md §4), per resource and calendar day ``d``::

    pool(d)   = on_hand_quantity + arrivals with date < d
                - (consumable only) quantities used by jobs before d
    demand(d) = sum of quantities required by jobs that use the resource on d

A day is covered when ``pool(d) >= demand(d)``. Reusable resources return the
day after the job, so for them nothing is subtracted for earlier jobs.
An arrival on ``d`` itself does not count for ``d``: setup happens that day.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .contracts import (
    BusinessSnapshot,
    DeliveryStatus,
    DependencyType,
    EntityType,
    NormalizedIncident,
)

INBOUND_STATUSES = {DeliveryStatus.scheduled, DeliveryStatus.delayed, DeliveryStatus.partially_delivered}
INACTIVE_JOB_STATUSES = {"cancelled", "completed"}


@dataclass
class Arrival:
    resource_id: str
    date: date
    quantity: int
    source_id: str  # delivery id, or the plan action id that adds it
    plan_added: bool = False
    # Units a plan rents or buys for the shortfall are only counted for the
    # shortfall dates: return terms are not recorded, so assuming they stay
    # would invent availability for later jobs.
    persistent: bool = True


@dataclass
class Demand:
    job_id: str
    resource_id: str
    date: date
    quantity: int  # quantity the job is allocated in this state
    required: int  # quantity recorded on the dependency (never changed by plans)
    dependency_id: str


@dataclass
class BusinessState:
    today: date
    snapshot: BusinessSnapshot
    arrivals: list[Arrival] = field(default_factory=list)
    demands: list[Demand] = field(default_factory=list)
    # The part of the disrupted delivery that now arrives late, if any.
    delayed_arrival: Arrival | None = None
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ build

    @classmethod
    def from_snapshot(cls, snapshot: BusinessSnapshot, incident: NormalizedIncident) -> "BusinessState":
        state = cls(today=business_today(snapshot, incident), snapshot=snapshot)
        disrupted = _find_disrupted_delivery(snapshot, incident)
        if disrupted is None and incident.delivery_id:
            state.notes.append(f"Incident delivery {incident.delivery_id} not found in snapshot.")

        for dl in snapshot.deliveries:
            if dl.status not in INBOUND_STATUSES:
                continue
            if disrupted is not None and dl.id == disrupted.id:
                late_qty = min(incident.quantity_affected or 0, dl.quantity)
                on_time = dl.quantity - late_qty
                if on_time:
                    state.arrivals.append(Arrival(dl.resource_id, dl.expected_date, on_time, dl.id))
                if late_qty and incident.new_arrival_date is not None:
                    late = Arrival(dl.resource_id, incident.new_arrival_date, late_qty, f"{dl.id}:delayed")
                    state.arrivals.append(late)
                    state.delayed_arrival = late
                continue
            state.arrivals.append(Arrival(dl.resource_id, dl.expected_date, dl.quantity, dl.id))

        jobs = {j.id: j for j in snapshot.jobs}
        for dep in snapshot.dependencies:
            if dep.dependency_type != DependencyType.requires or dep.target_type != EntityType.job:
                continue
            job = jobs.get(dep.target_id)
            if job is None or job.status in INACTIVE_JOB_STATUSES or dep.required_quantity is None:
                continue
            when = dep.required_by or job.event_date
            state.demands.append(
                Demand(job.id, dep.source_id, when, dep.required_quantity, dep.required_quantity, dep.id)
            )
        return state

    def copy(self) -> "BusinessState":
        new = BusinessState(
            today=self.today,
            snapshot=self.snapshot,  # read-only; never mutated
            arrivals=copy.deepcopy(self.arrivals),
            demands=copy.deepcopy(self.demands),
            notes=list(self.notes),
        )
        if self.delayed_arrival is not None:
            idx = next(i for i, a in enumerate(self.arrivals) if a is self.delayed_arrival)
            new.delayed_arrival = new.arrivals[idx]
        return new

    # ------------------------------------------------------------------ lookups

    def resource(self, resource_id: str):
        return next((r for r in self.snapshot.resources if r.id == resource_id), None)

    def supplier(self, supplier_id: str):
        return next((s for s in self.snapshot.suppliers if s.id == supplier_id), None)

    def job(self, job_id: str):
        return next((j for j in self.snapshot.jobs if j.id == job_id), None)

    def offerings(self, supplier_id: str | None = None, resource_id: str | None = None):
        return [
            o
            for o in self.snapshot.supplier_offerings
            if (supplier_id is None or o.supplier_id == supplier_id)
            and (resource_id is None or o.resource_id == resource_id)
        ]

    def commitments_for(self, job_id: str):
        return [c for c in self.snapshot.commitments if c.job_id == job_id]

    def demands_for(self, resource_id: str, job_id: str | None = None) -> list[Demand]:
        return [
            d for d in self.demands if d.resource_id == resource_id and (job_id is None or d.job_id == job_id)
        ]

    def spare_quantity(self, resource_id: str) -> int:
        """Units recorded as owned but not counted on hand (e.g. in storage elsewhere)."""
        r = self.resource(resource_id)
        return max(0, r.owned_quantity - r.on_hand_quantity) if r else 0

    # ------------------------------------------------------------------ inventory maths

    def demand_on(self, resource_id: str, day: date) -> int:
        return sum(d.quantity for d in self.demands_for(resource_id) if d.date == day)

    def demand_dates(self, resource_id: str) -> set[date]:
        return {d.date for d in self.demands_for(resource_id)}

    def pool(
        self,
        resource_id: str,
        day: date,
        *,
        target_dates: frozenset[date] = frozenset(),
        ignore_plan_timing: bool = False,
    ) -> int:
        """Units usable on ``day``.

        ``ignore_plan_timing`` counts every plan-added arrival regardless of its
        date. Comparing that with the timed pool separates "not enough units"
        (QUANTITY_SUFFICIENT) from "enough units, too late" (DEADLINE_MET).
        """
        resource = self.resource(resource_id)
        if resource is None:
            return 0
        total = resource.on_hand_quantity
        for a in self.arrivals:
            if a.resource_id != resource_id:
                continue
            if a.plan_added and not a.persistent and day not in target_dates:
                continue
            if (a.plan_added and ignore_plan_timing) or a.date < day:
                total += a.quantity
        if not resource.reusable:
            total -= sum(d.quantity for d in self.demands_for(resource_id) if d.date < day)
        return total


def business_today(snapshot: BusinessSnapshot, incident: NormalizedIncident | None = None) -> date:
    """The current business date: the later of snapshot ``as_of`` and the report time."""
    moments: list[datetime] = [snapshot.as_of]
    if incident is not None:
        moments.append(incident.reported_at)
    try:
        tz = ZoneInfo(snapshot.business.timezone)
        return max(m.astimezone(tz).date() for m in moments)
    except Exception:  # unknown zone: fall back to each timestamp's own offset
        return max(m.date() for m in moments)


def _find_disrupted_delivery(snapshot: BusinessSnapshot, incident: NormalizedIncident):
    if incident.delivery_id:
        return next((d for d in snapshot.deliveries if d.id == incident.delivery_id), None)
    # No delivery id: accept only an unambiguous match on supplier + resource (+ date).
    matches = [
        d
        for d in snapshot.deliveries
        if d.supplier_id == incident.supplier_id
        and d.resource_id == incident.resource_id
        and d.status in INBOUND_STATUSES
        and (incident.original_arrival_date is None or d.expected_date == incident.original_arrival_date)
    ]
    return matches[0] if len(matches) == 1 else None
