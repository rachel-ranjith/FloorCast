"""Topology read endpoint: the full Building > Suite > Row > Position floor.

Returns enough power data per position (its rack) and per row (load + capacity)
to both render the floor and colour it by load — empty positions included.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from floorcast.api.schemas import TopologyResponse
from floorcast.services.topology_service import TopologyService

router = APIRouter(tags=["topology"])


def get_topology_service() -> TopologyService:
    """Overridable in tests via app.dependency_overrides."""
    return TopologyService()


@router.get("/topology", response_model=TopologyResponse)
def get_topology(service: TopologyService = Depends(get_topology_service)) -> TopologyResponse:
    return service.get_topology()
