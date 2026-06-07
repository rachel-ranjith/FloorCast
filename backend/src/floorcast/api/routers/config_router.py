"""Expose the live, resolved configuration (read-only).

Lets the frontend show the rack catalog, power envelope, and optimizer knobs
that are actually in effect — confirming nothing is hardcoded.
"""

from __future__ import annotations

from fastapi import APIRouter

from config.settings import get_settings

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
def get_config() -> dict:
    return get_settings().model_dump(by_alias=True, mode="json")


@router.get("/rack-catalog")
def get_rack_catalog() -> dict:
    return get_settings().model_dump(by_alias=True, mode="json")["rack_catalog"]
