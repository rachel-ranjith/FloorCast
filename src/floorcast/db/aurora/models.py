"""SQLAlchemy ORM models mirroring schema.sql.

Kept deliberately in sync with schema.sql (which is the canonical DDL applied to
Aurora). Use these for application reads/writes; use schema.sql for migrations.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Maps to the native Postgres enum `run_status` already created by schema.sql.
# Values must match the DDL exactly. create_type=False so SQLAlchemy binds/casts
# to the existing type but never tries to CREATE/DROP it (schema.sql owns it).
RUN_STATUS_ENUM = PgEnum(
    "pending",
    "running",
    "optimal",
    "feasible",
    "infeasible",
    "failed",
    name="run_status",
    create_type=False,
)


class Scenario(Base):
    __tablename__ = "scenarios"

    scenario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    config_overrides: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_baseline: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OptimizationRun(Base):
    __tablename__ = "optimization_runs"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scenarios.scenario_id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(RUN_STATUS_ENUM, default="pending", nullable=False)

    horizon_months: Mapped[int] = mapped_column(Integer, nullable=False)
    objective_value: Mapped[float | None] = mapped_column(Float)
    total_swaps: Mapped[int | None] = mapped_column(Integer)
    # Aggregate soft row-tier overage (kW-months) across the plan; 0.0 when every
    # row stayed within headroom. Surfaced in the run list/detail read paths.
    total_row_overage_kw: Mapped[float | None] = mapped_column(Float)
    solver_wall_time_ms: Mapped[int | None] = mapped_column(Integer)

    resolved_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fleet_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    schedule: Mapped["Schedule"] = relationship(
        back_populates="run", uselist=False, cascade="all, delete-orphan"
    )


class Schedule(Base):
    __tablename__ = "schedules"

    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("optimization_runs.run_id", ondelete="CASCADE")
    )
    horizon_months: Mapped[int] = mapped_column(Integer, nullable=False)
    total_swaps: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("run_id"),)

    run: Mapped[OptimizationRun] = relationship(back_populates="schedule")
    items: Mapped[list["ScheduleItem"]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan"
    )


class ScheduleItem(Base):
    __tablename__ = "schedule_items"

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schedules.schedule_id", ondelete="CASCADE")
    )
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    position_id: Mapped[str] = mapped_column(Text, nullable=False)
    row_id: Mapped[str | None] = mapped_column(Text)
    suite_id: Mapped[str] = mapped_column(Text, nullable=False)
    building_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_rack_type: Mapped[str] = mapped_column(Text, nullable=False)
    to_rack_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_power_kw: Mapped[float] = mapped_column(Float, nullable=False)
    to_power_kw: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("month >= 1", name="ck_item_month"),)

    schedule: Mapped[Schedule] = relationship(back_populates="items")


class PowerUtilization(Base):
    __tablename__ = "power_utilization"

    util_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schedules.schedule_id", ondelete="CASCADE")
    )
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    tier_id: Mapped[str] = mapped_column(Text, nullable=False)
    load_kw: Mapped[float] = mapped_column(Float, nullable=False)
    capacity_kw: Mapped[float] = mapped_column(Float, nullable=False)
    utilization: Mapped[float] = mapped_column(Float, nullable=False)


class RackHistory(Base):
    __tablename__ = "rack_history"

    history_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rack_id: Mapped[str] = mapped_column(Text, nullable=False)
    position_id: Mapped[str] = mapped_column(Text, nullable=False)
    suite_id: Mapped[str] = mapped_column(Text, nullable=False)
    building_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    rack_type: Mapped[str | None] = mapped_column(Text)
    power_draw_kw: Mapped[float | None] = mapped_column(Float)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("optimization_runs.run_id", ondelete="SET NULL")
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
