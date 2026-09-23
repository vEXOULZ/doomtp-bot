"""/healthz (liveness), /readyz (readiness with component detail) and /metrics (ADR-0015)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import PlainTextResponse

from doomtp_bot import __version__
from doomtp_bot.core import metrics
from doomtp_bot.core.health import HealthRegistry, Status

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """200 while the event loop is responsive. Used by the Docker HEALTHCHECK."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    registry: HealthRegistry = request.app.state.health
    overall, components = await registry.snapshot()
    if overall is Status.UNHEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": overall.value,
        "version": __version__,
        "components": {name: {"status": c.status.value, **c.detail} for name, c in components.items()},
    }


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics_text() -> PlainTextResponse:
    """Counters in the Prometheus text format, for a scraper on the LAN (ADR-0015). No channel or user in it."""
    return PlainTextResponse(metrics.render(), media_type=metrics.CONTENT_TYPE)
