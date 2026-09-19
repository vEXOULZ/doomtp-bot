"""FastAPI application factory."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from doomtp_bot import __version__
from doomtp_bot.api.routes import auth, data, health, language
from doomtp_bot.core.health import HealthRegistry
from doomtp_bot.twitch.auth import TwitchAuth
from doomtp_bot.webui import pages
from doomtp_bot.webui.auth import AdminAuth
from doomtp_bot.webui.emoji import STATIC_DIR


def create_app(
    health_registry: HealthRegistry,
    twitch_auth: TwitchAuth | None = None,
    *,
    runtime: Any = None,
    policy: Any = None,
    services: dict[str, Any] | None = None,
    admin_password: str | None = None,
) -> FastAPI:
    app = FastAPI(title="doomtp-bot", version=__version__, docs_url="/docs", redoc_url=None)
    app.state.health = health_registry
    app.state.twitch_auth = twitch_auth
    app.state.runtime = runtime  # the language API parses and explains with the live registry
    app.state.policy = policy
    app.state.admin_auth = AdminAuth(password=admin_password)
    for name, service in (services or {}).items():  # customcmds, packs, triggers, filters
        setattr(app.state, name, service)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(language.router)
    app.include_router(data.router)
    app.include_router(pages.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
