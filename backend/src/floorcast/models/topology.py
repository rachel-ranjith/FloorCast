"""Topology domain models: Building > Suite > Row > RackPosition.

These are the logical hierarchy of a data centre floor. Live state for each
entity is persisted in DynamoDB (see db/dynamo/tables.py).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RackPosition(BaseModel):
    """A single slot on the floor that may hold one rack."""

    position_id: str
    row_id: str
    suite_id: str
    building_id: str
    ordinal: int = Field(description="0-based position index within the row")
    occupied: bool = False
    rack_id: str | None = None


class Row(BaseModel):
    row_id: str
    suite_id: str
    building_id: str
    ordinal: int
    label: str
    capacity_kw: float
    positions: list[RackPosition] = []


class Suite(BaseModel):
    suite_id: str
    building_id: str
    ordinal: int
    label: str
    capacity_mw: float
    rows: list[Row] = []


class Building(BaseModel):
    building_id: str
    label: str
    capacity_mw: float
    suites: list[Suite] = []
