"""The bot's own events, as triggers a channel can react to (ADR-0034).

- `bot_online`: the bot is in chat again, at start (`event.reason` `start`, or `update` when the version
  changed since the last run) or after Twitch dropped it and it came back (`reconnect`). `event.down_s` is
  how long it was gone, and `event.cause` why the connection was started again. A channel hears it at most every 15 minutes, whatever the trigger's own cooldown.
- `bot_updated`: the version changed since the last run, before that run's `bot_online`: `event.version`
  and `event.previous`.
- `bot_joined`: the bot was asked into the channel (`!join`, the API), not at every start.
- `bot_modded`, `bot_unmodded`: the capability probe saw the bot become, or stop being, a moderator there.

There is no `bot_offline`: a bot that is going away, or has gone, can't be counted on to say so.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import structlog

from doomtp_bot.clock import now_ms
from doomtp_bot.core.build import BUILD, BuildInfo
from doomtp_bot.storage.db import Connection, fetch_one

log = structlog.get_logger(__name__)

BOT_EVENT_TYPES = ("bot_online", "bot_updated", "bot_joined", "bot_modded", "bot_unmodded")
BEAT_S = 60.0

#: Runs a channel's triggers of one type with this payload: `Dispatcher.bot_event`.
Fire = Callable[[str, str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LastRun:
    version: str
    #: When it was last known to be up, epoch ms.
    alive_at: int


class BotRuns:
    """`bot_runs` in the chat log: this process's row, kept alive every minute, and the one before it."""

    def __init__(self, conn: Connection, build: BuildInfo = BUILD, clock: Callable[[], int] = now_ms) -> None:
        self.conn = conn
        self.build = build
        self.clock = clock
        self.id: int | None = None
        self._task: asyncio.Task[None] | None = None

    async def begin(self) -> LastRun | None:
        """Record this start and return the run before it, if there was one."""
        row = await fetch_one(self.conn, "SELECT version, alive_at FROM bot_runs ORDER BY id DESC LIMIT 1")
        now = self.clock()
        created = await fetch_one(
            self.conn,
            "INSERT INTO bot_runs (version, release, commit, started_at, alive_at) VALUES (%s, %s, %s, %s, %s)"
            " RETURNING id",
            (self.build.version, self.build.release, self.build.commit, now, now),
        )
        self.id = int(created["id"]) if created else None
        return LastRun(row["version"], int(row["alive_at"])) if row else None

    async def beat(self) -> None:
        if self.id is not None:
            await self.conn.execute("UPDATE bot_runs SET alive_at = %s WHERE id = %s", (self.clock(), self.id))

    async def end(self) -> None:
        await self.stop()
        if self.id is not None:
            now = self.clock()
            await self.conn.execute(
                "UPDATE bot_runs SET alive_at = %s, ended_at = %s WHERE id = %s", (now, now, self.id)
            )

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="bot-runs")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(BEAT_S)
            try:
                await self.beat()
            except Exception:
                log.exception("bot_runs.beat_failed")


class BotEvents:
    """Turns what the bot notices about itself into its triggers, in each channel it is in."""

    def __init__(self, fire: Fire, channels: Callable[[], Iterable[str]], build: BuildInfo = BUILD) -> None:
        self.fire = fire
        self.channels = channels
        self.build = build
        self._online_before = False

    async def online(
        self,
        last: LastRun | None,
        *,
        down_since_ms: int | None = None,
        cause: str | None = None,
        now: int | None = None,
    ) -> None:
        """The bot is in chat: the first time in this process (`last` is the run before), or again after
        Twitch dropped it (`down_since_ms`, when it last heard chat; `cause`, what the watchdog found)."""
        now = now_ms() if now is None else now
        updated = False
        if not self._online_before:
            self._online_before = True
            updated = last is not None and last.version != self.build.version
            reason = "update" if updated else "start"
            since = last.alive_at if last is not None else None
        else:
            reason, since = "reconnect", down_since_ms
        down_s = max(0, (now - since) // 1000) if since else None
        payload: dict[str, Any] = {"reason": reason, "down_s": down_s, "version": self.build.version}
        if reason == "reconnect":
            payload["cause"] = cause or "unknown"
        for channel_id in list(self.channels()):
            if updated and last is not None:
                await self._fire(channel_id, "bot_updated", {"version": self.build.version, "previous": last.version})
            await self._fire(channel_id, "bot_online", payload)

    async def joined(self, channel_id: str) -> None:
        await self._fire(channel_id, "bot_joined", {})

    async def moderator_changed(self, channel_id: str, modded: bool) -> None:
        await self._fire(channel_id, "bot_modded" if modded else "bot_unmodded", {})

    async def _fire(self, channel_id: str, type_: str, payload: dict[str, Any]) -> None:
        try:
            await self.fire(channel_id, type_, payload)
        except Exception:  # one channel's trigger never stops the others
            log.exception("bot_event.failed", channel=channel_id, type=type_)
