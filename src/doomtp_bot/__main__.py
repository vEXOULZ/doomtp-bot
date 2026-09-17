"""Entry point: config → lock → databases → health → API (→ Twitch, once implemented)."""

from __future__ import annotations

import asyncio
import sys

import structlog
import uvicorn

from doomtp_bot import __version__
from doomtp_bot.api.app import create_app
from doomtp_bot.config import Settings
from doomtp_bot.core.health import ComponentHealth, HealthRegistry, Status
from doomtp_bot.core.instance_lock import InstanceLock, InstanceLockError
from doomtp_bot.log import configure_logging
from doomtp_bot.storage.db import Databases, current_version

log = structlog.get_logger("doomtp_bot")


async def run(settings: Settings) -> None:
    dbs = await Databases.open(settings.bot_db_path, settings.chatlog_db_path)
    health = HealthRegistry()

    async def db_check() -> ComponentHealth:
        return ComponentHealth(
            Status.OK,
            {
                "bot_schema": await current_version(dbs.bot),
                "chatlog_schema": await current_version(dbs.chatlog),
            },
        )

    async def twitch_check() -> ComponentHealth:
        if not settings.twitch_configured:
            return ComponentHealth(Status.DISABLED, {"reason": "TWITCH_CLIENT_ID / secret not set"})
        return ComponentHealth(Status.DEGRADED, {"reason": "twitch adapter not implemented yet"})

    health.register("databases", db_check)
    health.register("twitch", twitch_check)

    app = create_app(health)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.web_host, port=settings.web_port, log_config=None, lifespan="on")
    )

    log.info("bot.start", version=__version__, api=f"http://{settings.web_host}:{settings.web_port}")
    try:
        # uvicorn owns SIGINT/SIGTERM handling and returns after a graceful shutdown.
        await server.serve()
    finally:
        await dbs.close()
        log.info("bot.stop")


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    lock = InstanceLock(settings.lock_path)
    try:
        lock.acquire()
    except InstanceLockError as exc:
        log.error("bot.already_running", detail=str(exc))
        sys.exit(1)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()


if __name__ == "__main__":
    main()
