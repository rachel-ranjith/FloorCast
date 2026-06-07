"""Rack domain models — the live, mutable fleet state."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class RackState(str, Enum):
    ACTIVE = "active"
    DECOMMISSIONING = "decommissioning"
    EMPTY = "empty"
    RESERVED = "reserved"


class Rack(BaseModel):
    """A physical rack installed at a position.

    `rack_type` is a key into the configurable rack_catalog (config/floorcast.yaml).
    `power_draw_kw` is denormalized from the catalog at install time so historical
    state stays accurate even if the catalog later changes.
    """

    rack_id: str
    position_id: str
    row_id: str
    suite_id: str
    building_id: str

    rack_type: str
    family: str
    generation: int
    power_draw_kw: float = Field(gt=0)

    state: RackState = RackState.ACTIVE
    installed_at: datetime
    updated_at: datetime
