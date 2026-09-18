"""FastAPI application factory."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from doomtp_bot import __version__
from doomtp_bot.api.routes import auth, health, language
from doomtp_bot.core.health import HealthRegistry
from doomtp_bot.twitch.auth import TwitchAuth


def create_app(
    health_registry: HealthRegistry,
    twitch_auth: TwitchAuth | None = None,
    *,
    runtime: Any = None,
    policy: Any = None,
) -> FastAPI:
    app = FastAPI(title="doomtp-bot", version=__version__, docs_url="/docs", redoc_url=None)
    app.state.health = health_registry
    app.state.twitch_auth = twitch_auth
    app.state.runtime = runtime  # the language API parses and explains with the live registry
    app.state.policy = policy
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(language.router)
    return app
