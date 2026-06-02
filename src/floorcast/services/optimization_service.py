"""Orchestrates an optimization run: load fleet -> solve -> persist.

What-if scenarios are expressed as `config_overrides`, a partial dict deep-merged
on top of the global Settings before solving. This keeps every constraint
overridable per-run without mutating global config.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

from config.settings import Settings, get_settings
from floorcast.db.aurora.models import (
    OptimizationRun,
    PowerUtilization,
    Schedule,
    ScheduleItem,
)
from floorcast.db.aurora.session import get_session
from floorcast.db.dynamo.repository import FloorRepository
from floorcast.models.optimization import OptimizationRequest, OptimizationResult, RunStatus
from floorcast.optimizer.engine import FleetInput, RackReplacementOptimizer


def _deep_merge(base: dict, overrides: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_settings(base: Settings, overrides: dict) -> Settings:
    """Return a new Settings with `overrides` deep-merged in (non-mutating)."""
    if not overrides:
        return base
    merged = _deep_merge(base.model_dump(by_alias=True), overrides)
    return Settings(**merged)


class OptimizationService:
    def __init__(self, settings: Settings | None = None, repo: FloorRepository | None = None):
        self.settings = settings or get_settings()
        self.repo = repo or FloorRepository(self.settings)

    def run(
        self, request: OptimizationRequest, fleet: FleetInput | None = None
    ) -> OptimizationResult:
        effective = resolve_settings(self.settings, request.config_overrides)
        fleet = fleet or self.repo.load_fleet_input()

        run_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)

        optimizer = RackReplacementOptimizer(effective)
        result = optimizer.solve(fleet, run_id=run_id, created_at=created_at)

        self._persist(request, effective, result)
        return result

    # ------------------------------------------------------------------ #
    def _persist(
        self, request: OptimizationRequest, effective: Settings, result: OptimizationResult
    ) -> None:
        with get_session(self.settings) as session:
            run = OptimizationRun(
                run_id=uuid.UUID(result.run_id),
                name=request.name,
                scenario_id=uuid.UUID(request.scenario_id) if request.scenario_id else None,
                status=result.status.value,
                horizon_months=result.horizon_months,
                objective_value=result.objective_value,
                total_swaps=result.total_swaps,
                total_row_overage_kw=result.total_row_overage_kw,
                solver_wall_time_ms=result.solver_wall_time_ms,
                resolved_config=effective.model_dump(by_alias=True, mode="json"),
                completed_at=datetime.now(timezone.utc),
            )
            session.add(run)

            if result.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE):
                schedule = Schedule(
                    run_id=run.run_id,
                    horizon_months=result.horizon_months,
                    total_swaps=result.total_swaps,
                )
                session.add(schedule)
                session.flush()  # populate schedule_id

                for plan in result.plan:
                    for swap in plan.swaps:
                        session.add(
                            ScheduleItem(
                                schedule_id=schedule.schedule_id,
                                month=swap.month,
                                position_id=swap.position_id,
                                row_id=swap.row_id,
                                suite_id=swap.suite_id,
                                building_id=swap.building_id,
                                from_rack_type=swap.from_rack_type,
                                to_rack_type=swap.to_rack_type,
                                from_power_kw=swap.from_power_kw,
                                to_power_kw=swap.to_power_kw,
                            )
                        )
                    self._persist_utilization(session, schedule, plan, effective)

            session.commit()

    @staticmethod
    def _persist_utilization(session, schedule, plan, effective: Settings) -> None:
        bld_cap = effective.power.building_capacity_mw * 1000.0
        suite_cap = effective.power.suite_capacity_mw * 1000.0
        for tier_id, util in plan.building_peak_util.items():
            session.add(
                PowerUtilization(
                    schedule_id=schedule.schedule_id, month=plan.month,
                    tier="building", tier_id=tier_id,
                    load_kw=util * bld_cap, capacity_kw=bld_cap, utilization=util,
                )
            )
        for tier_id, util in plan.suite_peak_util.items():
            session.add(
                PowerUtilization(
                    schedule_id=schedule.schedule_id, month=plan.month,
                    tier="suite", tier_id=tier_id,
                    load_kw=util * suite_cap, capacity_kw=suite_cap, utilization=util,
                )
            )
