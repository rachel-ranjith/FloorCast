"""Optimization-run read endpoints: list history and fetch a full schedule."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from floorcast.api.schemas import RunDetail, RunListResponse
from floorcast.services.run_service import RunNotFoundError, RunService

router = APIRouter(prefix="/runs", tags=["runs"])


def get_run_service() -> RunService:
    """Overridable in tests via app.dependency_overrides."""
    return RunService()


@router.get("", response_model=RunListResponse)
def list_runs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: RunService = Depends(get_run_service),
) -> RunListResponse:
    runs = service.list_runs(limit=limit, offset=offset)
    return RunListResponse(runs=runs, limit=limit, offset=offset)


@router.get("/{run_id}", response_model=RunDetail)
def get_run(
    # Typing as UUID makes a malformed id a clean 422 (not a 500); a well-formed
    # but unknown id falls through to a 404 below.
    run_id: uuid.UUID,
    service: RunService = Depends(get_run_service),
) -> RunDetail:
    try:
        return service.get_run(run_id)
    except RunNotFoundError:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
