"""Entry point: wires storage, policy, runtime, chat log, Twitch and the API into one process (ADR-0004)."""

from __future__ import annotations

import asyncio
import contextlib
import sys

import structlog
import uvicorn

from doomtp_bot import __version__
from doomtp_bot.api.app import create_app
from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.config import Settings
from doomtp_bot.core.capabilities import CapabilityProbe
from doomtp_bot.core.channels import ChannelManager
from doomtp_bot.core.dispatch import Dispatcher
from doomtp_bot.core.events import Event
from doomtp_bot.core.health import ComponentHealth, HealthRegistry, Status
from doomtp_bot.core.instance_lock import InstanceLock, InstanceLockError
from doomtp_bot.core.outbox import Outbox, SendResult
from doomtp_bot.core.streams import StreamPoller, StreamStatus
from doomtp_bot.customcmds.packs import PackService
from doomtp_bot.customcmds.resolution import CustomCommandLoader
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.filters.service import FilterService
from doomtp_bot.history.backfill import BackfillService
from doomtp_bot.history.provider import RecentMessagesProvider
from doomtp_bot.log import configure_logging
from doomtp_bot.moderation.automod import AutoMod
from doomtp_bot.moderation.index import ModerationIndex
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.roles import MODERATOR_RANK
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.storage.db import Databases, current_version
from doomtp_bot.triggers.runner import TriggerRunner
from doomtp_bot.triggers.service import TriggerService
from doomtp_bot.triggers.timers import ChatActivity, TimerScheduler
from doomtp_bot.twitch.auth import AuthorizedAccount, TwitchAuth, TwitchOAuthHttp
from doomtp_bot.twitch.client import TwitchService
from doomtp_bot.twitch.tokens import TokenStore
from doomtp_bot.variables.access import VariableAccessPolicy
from doomtp_bot.variables.store import SqliteVariableStore

log = structlog.get_logger("doomtp_bot")


class _NoSender:
    async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
        return SendResult(None, "not_connected")


async def run(settings: Settings) -> None:
    dbs = await Databases.open(settings.bot_db_path, settings.chatlog_db_path)
    health = HealthRegistry()

    policy = PolicyService(dbs.bot, bot_owner_ids=settings.bot_owner_ids)
    await policy.reload()
    store = SqliteVariableStore(dbs.bot)
    access = VariableAccessPolicy(policy, dbs.bot)
    await access.reload()
    customcmds = CustomCommandService(dbs.bot, on_grants_changed=access.reload)
    packs = PackService(dbs.bot, customcmds)
    content_filter = FilterService(dbs.bot)
    await content_filter.reload()
    history = RecentMessagesProvider(settings.history_provider_url)
    triggers = TriggerService(dbs.bot)
    await triggers.reload()
    activity = ChatActivity()
    writer = ChatLogWriter(dbs.chatlog)
    stale = await writer.close_stale_sessions()
    if stale:
        log.warning("chatlog.unclean_shutdown_detected", sessions_closed=stale)
    writer.start()
    moderation = ModerationIndex()

    dispatcher: Dispatcher | None = None

    async def sink(event: Event) -> None:
        if dispatcher is not None:
            await dispatcher.handle(event)

    twitch: TwitchService | None = None
    secret = settings.client_secret()
    if settings.twitch_client_id and secret:
        twitch = TwitchService(
            client_id=settings.twitch_client_id, client_secret=secret, tokens=TokenStore(dbs.bot), sink=sink
        )

    streams = StreamStatus()
    channels = ChannelManager(policy, twitch, writer, default_prefix=settings.default_prefix)
    probe = CapabilityProbe(policy=policy, channels=channels, prober=twitch)
    channels.on_joined = probe.probe  # a channel is probed as soon as its subscriptions are up
    services: dict[str, object] = {
        "policy": policy,
        "variable_store": store,
        "channels": channels,
        "customcmds": customcmds,
        "packs": packs,
        "filters": content_filter,
        "triggers": triggers,
        "variable_access": access,
    }
    if twitch is not None:
        services.update(twitch=twitch, login_for=twitch.login_for)
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        callbacks=policy,
        store=store,
        access=access,
        resolve_user=twitch.resolve_user if twitch else None,
        custom=CustomCommandLoader(customcmds, packs),
        services=services,
    )

    def rate_for(channel_id: str) -> tuple[int, float]:
        """Moderator send limits when the channel tier or the bot's role there allows them."""
        settings_ = policy.channel_settings(channel_id)
        elevated = settings_ is not None and settings_.tier in ("moderator", "full")
        if not elevated and twitch is not None and twitch.bot_id is not None:
            elevated = (
                policy.build_chatter(channel_id, twitch.bot_id, twitch.bot_login or "").rank >= MODERATOR_RANK
            )
        return (90, 30.0) if elevated else (20, 30.0)

    def hold_ms_for(channel_id: str) -> int:
        settings_ = policy.channel_settings(channel_id)
        return settings_.reply_hold_ms if settings_ else 0

    outbox = Outbox(
        twitch or _NoSender(),
        writer,
        rate_for=rate_for,
        hold_ms_for=hold_ms_for,
        content_filter=content_filter.apply,
    )
    trigger_runner = TriggerRunner(runtime=runtime, policy=policy, outbox=outbox)
    timers = TimerScheduler(
        triggers=triggers, runner=trigger_runner, policy=policy, activity=activity, streams=streams
    )
    dispatcher = Dispatcher(
        runtime=runtime,
        policy=policy,
        writer=writer,
        outbox=outbox,
        moderation=moderation,
        channels=channels,
        triggers=triggers,
        trigger_runner=trigger_runner,
        activity=activity,
        streams=streams,
        automod=(
            AutoMod(policy=policy, filters=content_filter, moderator=twitch) if twitch is not None else None
        ),
    )

    backfill = BackfillService(conn=dbs.chatlog, writer=writer, provider=history, policy=policy)
    poller = (
        StreamPoller(source=twitch, channels=channels, status=streams, sink=dispatcher.handle)
        if twitch is not None
        else None
    )

    async def start_twitch() -> None:
        if twitch is None:
            return
        try:
            if await twitch.start() and twitch.bot_id and twitch.bot_login:
                await channels.ensure_home(twitch.bot_id, twitch.bot_login)
                await channels.subscribe_all()
                await probe.probe_all()  # tier per channel, before anything checks a capability
                probe.start()
                if poller is not None:
                    await poller.poll()
                    poller.start()
                filled = await backfill.run_all()  # coverage gaps since the last run (ADR-0008)
                if filled:
                    log.info("history.startup_backfill", gaps=len(filled))
                backfill.start_keep_warm()
        except Exception:
            log.exception("twitch.start_failed")

    async def on_bot_authorized(account: AuthorizedAccount) -> None:
        log.info("twitch.authorized", bot=account.login, scopes=list(account.scopes))
        asyncio.create_task(start_twitch())  # noqa: RUF006 - fire and forget; errors are logged inside

    auth: TwitchAuth | None = None
    if twitch is not None and settings.twitch_client_id and secret:
        auth = TwitchAuth(
            client_id=settings.twitch_client_id,
            redirect_uri=settings.public_base_url.rstrip("/") + "/auth/callback",
            tokens=twitch.tokens,
            http=TwitchOAuthHttp(settings.twitch_client_id, secret),
            on_bot_authorized=on_bot_authorized,
            expected_bot_id=settings.twitch_bot_id,
        )

    async def db_check() -> ComponentHealth:
        return ComponentHealth(
            Status.OK,
            {
                "bot_schema": await current_version(dbs.bot),
                "chatlog_schema": await current_version(dbs.chatlog),
            },
        )

    async def chatlog_check() -> ComponentHealth:
        status = Status.OK if writer.queue_depth < 5_000 else Status.DEGRADED
        return ComponentHealth(
            status, {"queue_depth": writer.queue_depth, "last_flush_ms": writer.last_flush_ms}
        )

    async def twitch_check() -> ComponentHealth:
        if twitch is None:
            return ComponentHealth(Status.DISABLED, {"reason": "TWITCH_CLIENT_ID / secret not set"})
        return await twitch.health()

    health.register("databases", db_check)
    health.register("chatlog", chatlog_check)
    health.register("twitch", twitch_check)

    app = create_app(
        health,
        auth,
        runtime=runtime,
        policy=policy,
        services={
            "customcmds": customcmds,
            "packs": packs,
            "triggers": triggers,
            "filters": content_filter,
            "health": health,
            "channels": channels,
            "twitch": twitch,
            "variable_store": store,
            "chatlog_db": dbs.chatlog,
            "api_keys": ApiKeyService(dbs.bot),
        },
        admin_password=settings.admin_password_value(),
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.web_host, port=settings.web_port, log_config=None, lifespan="on")
    )
    log.info("bot.start", version=__version__, api=f"http://{settings.web_host}:{settings.web_port}")
    timers.start()
    twitch_start = asyncio.create_task(start_twitch())
    try:
        await server.serve()  # uvicorn owns SIGINT/SIGTERM and returns after a graceful shutdown
    finally:
        await timers.stop()
        await probe.stop()
        if poller is not None:
            await poller.stop()
        await backfill.stop()
        await history.close()
        twitch_start.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await twitch_start
        await dispatcher.drain()
        await writer.end_all_sessions("shutdown")
        if twitch is not None:
            await twitch.stop()
        await writer.stop()
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
