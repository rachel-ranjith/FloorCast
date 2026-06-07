"""Optimization endpoints: kick off a run (optionally a what-if scenario)."""

from __future__ import annotations

from fastapi import APIRouter

from floorcast.models.optimization import OptimizationRequest, OptimizationResult
from floorcast.services.optimization_service import OptimizationService

router = APIRouter(prefix="/optimize", tags=["optimization"])


@router.post("", response_model=OptimizationResult)
def run_optimization(request: OptimizationRequest) -> OptimizationResult:
    """Run the ILP optimizer and persist the resulting 12-month schedule.

    Pass `config_overrides` to run a what-if scenario, e.g.:
        {"name": "tighter headroom", "config_overrides": {"power": {"headroom_pct": 0.30}}}
    """
    return OptimizationService().run(request)
