"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from doomtp_bot import __version__
from doomtp_bot.api.routes import health
from doomtp_bot.core.health import HealthRegistry


def create_app(health_registry: HealthRegistry) -> FastAPI:
    app = FastAPI(title="doomtp-bot", version=__version__, docs_url="/docs", redoc_url=None)
    app.state.health = health_registry
    app.include_router(health.router)
    return app
