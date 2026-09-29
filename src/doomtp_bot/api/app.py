"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from doomtp_bot import __version__
from doomtp_bot.api.routes import auth, bot, commands, data, health, language, manage, session, site
from doomtp_bot.api.sessions import LOCAL_NETWORKS, AdminAuth, LoginLimiter, ReadLimiter, parse_networks
from doomtp_bot.core.health import HealthRegistry
from doomtp_bot.twitch.auth import TwitchAuth

# The editor bundle and the railroad diagrams, which the web site (doomtp-web) loads from here (ADR-0016).
STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    health_registry: HealthRegistry,
    twitch_auth: TwitchAuth | None = None,
    *,
    runtime: Any = None,
    policy: Any = None,
    services: dict[str, Any] | None = None,
    admin_password: str | None = None,
    admin_password_networks: str = LOCAL_NETWORKS,
) -> FastAPI:
    app = FastAPI(title="doomtp-bot", version=__version__, docs_url="/docs", redoc_url=None)
    app.state.health = health_registry
    app.state.twitch_auth = twitch_auth
    app.state.runtime = runtime  # the language API parses and explains with the live registry
    app.state.policy = policy
    app.state.admin_auth = AdminAuth(password=admin_password)
    app.state.admin_password_networks = parse_networks(admin_password_networks)
    app.state.login_limiter = LoginLimiter()  # failed JSON logins, per client address
    app.state.public_read_limiter = ReadLimiter()  # public chat log reads, per client address (ADR-0026)
    for name, service in (services or {}).items():  # customcmds, packs, triggers, filters
        setattr(app.state, name, service)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(language.router)
    app.include_router(data.router)
    app.include_router(manage.router)
    app.include_router(commands.router)
    app.include_router(bot.router)
    app.include_router(session.router)
    app.include_router(site.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
