"""Floor heatmap endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from floorcast.services.heatmap_service import HeatmapService

router = APIRouter(prefix="/heatmap", tags=["heatmap"])


@router.get("/buildings/{building_id}")
def building_heatmap(building_id: str) -> dict:
    return HeatmapService().building_heatmap(building_id)
