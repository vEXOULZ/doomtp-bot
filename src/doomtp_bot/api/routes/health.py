"""/healthz (liveness) and /readyz (readiness with component detail)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status

from doomtp_bot import __version__
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
