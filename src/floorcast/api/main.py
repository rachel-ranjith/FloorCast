"""FastAPI application entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from config.settings import get_settings
from floorcast.api.routers import config_router, heatmap, optimization

settings = get_settings()

app = FastAPI(
    title="Floorcast API",
    version="0.1.0",
    description="Data centre rack replacement optimizer and floor visualizer",
)

app.include_router(config_router.router)
app.include_router(heatmap.router)
app.include_router(optimization.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "env": settings.env}
