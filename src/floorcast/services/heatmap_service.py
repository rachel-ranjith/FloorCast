"""Builds the floor heatmap: per-position power draw across a building/suite.

The frontend renders rows x positions as a grid coloured by power_draw_kw.
"""

from __future__ import annotations

from config.settings import Settings, get_settings
from floorcast.db.dynamo.repository import FloorRepository


class HeatmapService:
    def __init__(self, settings: Settings | None = None, repo: FloorRepository | None = None):
        self.settings = settings or get_settings()
        self.repo = repo or FloorRepository(self.settings)

    def building_heatmap(self, building_id: str) -> dict:
        """Cells keyed by position_id with power + suite/row coordinates."""
        cells = []
        max_kw = 0.0
        for rack in self.repo.scan_racks():
            if rack.building_id != building_id:
                continue
            max_kw = max(max_kw, rack.power_draw_kw)
            cells.append(
                {
                    "position_id": rack.position_id,
                    "suite_id": rack.suite_id,
                    "row_id": rack.row_id,
                    "rack_type": rack.rack_type,
                    "generation": rack.generation,
                    "power_draw_kw": rack.power_draw_kw,
                }
            )
        return {
            "building_id": building_id,
            "max_power_kw": max_kw,
            "row_capacity_kw": self.settings.power.row_capacity_kw,
            "cells": cells,
        }
