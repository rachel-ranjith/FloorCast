"""Read-back of optimization runs and their schedules from Aurora.

The write side lives in optimization_service.py; this is the reverse path the
frontend uses to list run history and re-render a chosen run's 12-month plan.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from config.settings import Settings, get_settings
from floorcast.api.schemas import (
    PowerUtilizationOut,
    RunDetail,
    RunSummary,
    ScheduleItemOut,
    ScheduleOut,
)
from floorcast.db.aurora.models import (
    OptimizationRun,
    PowerUtilization,
    Schedule,
    ScheduleItem,
)
from floorcast.db.aurora.session import get_session


class RunNotFoundError(Exception):
    """Raised when a (well-formed) run_id has no matching run."""

    def __init__(self, run_id: uuid.UUID):
        self.run_id = run_id
        super().__init__(f"run {run_id} not found")


class RunService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def list_runs(self, limit: int = 50, offset: int = 0) -> list[RunSummary]:
        """Most-recent-first page of runs for the history list.

        Ordered by created_at DESC (run_id as a stable tiebreaker). Paginated
        with LIMIT/OFFSET — fine for a human-scale run history; if this ever
        grows huge, switch to keyset pagination on created_at.
        """
        with get_session(self.settings) as session:
            stmt = (
                select(OptimizationRun)
                .order_by(
                    OptimizationRun.created_at.desc(),
                    OptimizationRun.run_id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
            runs = session.execute(stmt).scalars().all()
            return [self._summary(r) for r in runs]

    def get_run(self, run_id: uuid.UUID) -> RunDetail:
        with get_session(self.settings) as session:
            run = session.get(OptimizationRun, run_id)
            if run is None:
                raise RunNotFoundError(run_id)

            schedule = session.execute(
                select(Schedule).where(Schedule.run_id == run_id)
            ).scalar_one_or_none()

            schedule_out: ScheduleOut | None = None
            if schedule is not None:
                items = session.execute(
                    select(ScheduleItem)
                    .where(ScheduleItem.schedule_id == schedule.schedule_id)
                    .order_by(
                        ScheduleItem.month,
                        ScheduleItem.suite_id,
                        ScheduleItem.position_id,
                    )
                ).scalars().all()
                utils = session.execute(
                    select(PowerUtilization)
                    .where(PowerUtilization.schedule_id == schedule.schedule_id)
                    .order_by(
                        PowerUtilization.month,
                        PowerUtilization.tier,
                        PowerUtilization.tier_id,
                    )
                ).scalars().all()
                schedule_out = self._schedule(schedule, items, utils)

            return self._detail(run, schedule_out)

    # ------------------------------------------------------------------ #
    # ORM -> response-schema mapping
    # ------------------------------------------------------------------ #
    @staticmethod
    def _summary(run: OptimizationRun) -> RunSummary:
        return RunSummary(
            run_id=str(run.run_id),
            name=run.name,
            status=run.status,
            total_swaps=run.total_swaps,
            total_row_overage_kw=run.total_row_overage_kw,
            created_at=run.created_at,
        )

    @staticmethod
    def _schedule(
        schedule: Schedule,
        items: list[ScheduleItem],
        utils: list[PowerUtilization],
    ) -> ScheduleOut:
        items_by_month: dict[str, list[ScheduleItemOut]] = {}
        for it in items:
            items_by_month.setdefault(str(it.month), []).append(
                ScheduleItemOut(
                    item_id=str(it.item_id),
                    month=it.month,
                    position_id=it.position_id,
                    row_id=it.row_id,
                    suite_id=it.suite_id,
                    building_id=it.building_id,
                    from_rack_type=it.from_rack_type,
                    to_rack_type=it.to_rack_type,
                    from_power_kw=it.from_power_kw,
                    to_power_kw=it.to_power_kw,
                )
            )
        power_utilization = [
            PowerUtilizationOut(
                month=u.month,
                tier=u.tier,
                tier_id=u.tier_id,
                load_kw=u.load_kw,
                capacity_kw=u.capacity_kw,
                utilization=u.utilization,
            )
            for u in utils
        ]
        return ScheduleOut(
            schedule_id=str(schedule.schedule_id),
            horizon_months=schedule.horizon_months,
            total_swaps=schedule.total_swaps,
            items_by_month=items_by_month,
            power_utilization=power_utilization,
        )

    @staticmethod
    def _detail(run: OptimizationRun, schedule: ScheduleOut | None) -> RunDetail:
        return RunDetail(
            run_id=str(run.run_id),
            name=run.name,
            status=run.status,
            horizon_months=run.horizon_months,
            objective_value=run.objective_value,
            total_swaps=run.total_swaps,
            total_row_overage_kw=run.total_row_overage_kw,
            solver_wall_time_ms=run.solver_wall_time_ms,
            error_message=run.error_message,
            created_at=run.created_at,
            completed_at=run.completed_at,
            schedule=schedule,
        )
