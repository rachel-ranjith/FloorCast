"""Optimization request/result models — persisted to Aurora."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    FAILED = "failed"


class OptimizationRequest(BaseModel):
    """Inputs for an optimization run.

    `config_overrides` lets a what-if scenario tweak any constraint without
    editing the global config (e.g. raise headroom_pct, lower MW limits).
    """

    name: str
    scenario_id: str | None = None
    config_overrides: dict = Field(default_factory=dict)


class ScheduledSwap(BaseModel):
    """A single rack replacement the optimizer scheduled in a given month."""

    position_id: str
    suite_id: str
    building_id: str
    from_rack_type: str
    to_rack_type: str
    from_power_kw: float
    to_power_kw: float
    month: int = Field(ge=1, description="1-based month within the horizon")


class MonthlyPlan(BaseModel):
    month: int
    swaps: list[ScheduledSwap] = []
    # Peak utilization (load / capacity) reached this month, per tier.
    building_peak_util: dict[str, float] = {}
    suite_peak_util: dict[str, float] = {}
    # Total row-tier overage (kW above the row headroom threshold) this month.
    # Row headroom is a soft constraint; this is what the optimizer minimizes.
    row_overage_kw: float = 0.0


class OptimizationResult(BaseModel):
    run_id: str
    status: RunStatus
    objective_value: float | None = None
    horizon_months: int
    total_swaps: int
    plan: list[MonthlyPlan] = []
    # Sum of row-tier overage (kW-months) across the whole plan; 0.0 when every
    # row stays within its (soft) headroom threshold.
    total_row_overage_kw: float = 0.0
    solver_wall_time_ms: int | None = None
    created_at: datetime
