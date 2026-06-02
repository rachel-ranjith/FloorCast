"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config.settings import get_settings
from floorcast.api.routers import config_router, heatmap, optimization, runs, topology

logger = logging.getLogger("floorcast.api")

settings = get_settings()

app = FastAPI(
    title="Floorcast API",
    version="0.1.0",
    description="Data centre rack replacement optimizer and floor visualizer",
)

# CORS: the #1 frontend blocker. Origins come from settings (localhost dev ports
# by default; override the full list via FLOORCAST_CORS__ALLOW_ORIGINS).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors.allow_origins,
    allow_credentials=settings.cors.allow_credentials,
    allow_methods=settings.cors.allow_methods,
    allow_headers=settings.cors.allow_headers,
)

app.include_router(config_router.router)
app.include_router(heatmap.router)
app.include_router(optimization.router)
app.include_router(topology.router)
app.include_router(runs.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler so an unexpected error (e.g. DB unavailable) returns a
    clean JSON 500 instead of leaking a stack trace to the client. HTTPException
    and request-validation errors keep their own (more specific) handlers."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "env": settings.env}
