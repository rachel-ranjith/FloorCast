"""Response schemas for the read-side API.

These are the wire contracts the frontend consumes — deliberately separate from
the domain models (floorcast.models.*) and the ORM (db.aurora.models) so the
internal shapes can evolve without breaking the API, and so UUID/Decimal/datetime
all serialize to clean JSON.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


# --------------------------------------------------------------------------- #
# Topology (GET /topology) — Building > Suite > Row > Position, with each
# occupied position carrying its current rack's power so the frontend can both
# render the floor and colour it by load. Empty positions come back with
# occupied=false and rack=null.
# --------------------------------------------------------------------------- #
class RackSummary(BaseModel):
    rack_id: str
    rack_type: str
    family: str
    generation: int
    power_draw_kw: float


class PositionNode(BaseModel):
    position_id: str
    ordinal: int
    occupied: bool
    # Mapped from the stored `occupant_rack_id` attribute back onto rack_id
    # (the attribute was renamed to dodge the gsi-generation null-key bug).
    rack_id: str | None = None
    rack: RackSummary | None = None


class RowNode(BaseModel):
    row_id: str
    label: str
    ordinal: int
    capacity_kw: float
    load_kw: float  # sum of occupant power in this row — drives the heat colour
    positions: list[PositionNode] = []


class SuiteNode(BaseModel):
    suite_id: str
    label: str
    ordinal: int
    capacity_mw: float
    capacity_kw: float
    load_kw: float
    rows: list[RowNode] = []


class BuildingNode(BaseModel):
    building_id: str
    label: str
    capacity_mw: float
    capacity_kw: float
    load_kw: float
    suites: list[SuiteNode] = []


class TopologyResponse(BaseModel):
    buildings: list[BuildingNode] = []
    # Echo the configured row capacity so a frontend can compute utilization
    # for empty rows too (load_kw / row_capacity_kw) without a config call.
    row_capacity_kw: float


# --------------------------------------------------------------------------- #
# Runs (GET /runs, GET /runs/{run_id})
# --------------------------------------------------------------------------- #
class RunSummary(BaseModel):
    run_id: str
    name: str
    status: str
    total_swaps: int | None = None
    total_row_overage_kw: float | None = None
    created_at: datetime


class RunListResponse(BaseModel):
    runs: list[RunSummary] = []
    limit: int
    offset: int


class ScheduleItemOut(BaseModel):
    item_id: str
    month: int
    position_id: str
    row_id: str | None = None
    suite_id: str
    building_id: str
    from_rack_type: str
    to_rack_type: str
    from_power_kw: float
    to_power_kw: float


class PowerUtilizationOut(BaseModel):
    month: int
    tier: str  # 'building' | 'suite' | 'row'
    tier_id: str
    load_kw: float
    capacity_kw: float
    utilization: float


class ScheduleOut(BaseModel):
    schedule_id: str
    horizon_months: int
    total_swaps: int
    # Swaps grouped by month, keyed by the (string) month number for a 12-month
    # timeline. JSON object keys are strings, so "1".."12".
    items_by_month: dict[str, list[ScheduleItemOut]] = {}
    power_utilization: list[PowerUtilizationOut] = []


class RunDetail(BaseModel):
    run_id: str
    name: str
    status: str
    horizon_months: int
    objective_value: float | None = None
    total_swaps: int | None = None
    total_row_overage_kw: float | None = None
    solver_wall_time_ms: int | None = None
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    # null for runs that produced no schedule (infeasible / failed).
    schedule: ScheduleOut | None = None
