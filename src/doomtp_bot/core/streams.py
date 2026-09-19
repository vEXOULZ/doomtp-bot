"""Who is live, by polling Helix (ADR-0007, architecture §10).

EventSub's `stream.online` costs nothing only with a broadcaster token, which the basic tier doesn't
have. So the bot asks Helix `Get Streams` for every joined channel at once, every minute, and turns the
differences into `StreamStatusChanged` events. That is one request per minute no matter how many
channels are joined, and it works in a channel where nobody authorized anything.

Live state is memory-only on purpose: it is stale the moment the process stops, and the first poll
after a restart rebuilds it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from doomtp_bot.clock import now_ms
from doomtp_bot.core.events import StreamStatusChanged

log = structlog.get_logger(__name__)

INTERVAL_S = 60.0
BATCH = 100  # Helix `Get Streams` takes up to 100 user_ids per request


class LiveSource(Protocol):
    async def fetch_live(self, channel_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Live channels among these ids, as id → {title, game, viewers, started_at}. Raises on failure."""
        ...


class JoinedChannels(Protocol):
    def active_channels(self) -> list[Any]: ...


@dataclass
class StreamStatus:
    """The live set, shared with anything that cares (timers, triggers, the admin page)."""

    streams: dict[str, dict[str, Any]] = field(default_factory=dict)

    def is_live(self, channel_id: str) -> bool:
        return channel_id in self.streams

    def info(self, channel_id: str) -> dict[str, Any]:
        return dict(self.streams.get(channel_id, {}))

    @property
    def live_ids(self) -> frozenset[str]:
        return frozenset(self.streams)


class StreamPoller:
    """One task that asks Helix who is live and reports the changes."""

    def __init__(
        self,
        *,
        source: LiveSource,
        channels: JoinedChannels,
        status: StreamStatus,
        sink: Callable[[StreamStatusChanged], Awaitable[None]] | None = None,
        interval_s: float = INTERVAL_S,
    ) -> None:
        self.source = source
        self.channels = channels
        self.status = status
        self.sink = sink
        self.interval_s = interval_s
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="stream-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll()
            except Exception:  # one bad poll must not end the loop
                log.exception("streams.poll_failed")
            await asyncio.sleep(self.interval_s)

    async def poll(self) -> list[StreamStatusChanged]:
        """One sweep. Returns the changes, which is what makes this testable without sleeping."""
        wanted = [c.channel_id for c in self.channels.active_channels()]
        if not wanted:
            self.status.streams.clear()
            return []
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(wanted), BATCH):
            try:
                found.update(await self.source.fetch_live(wanted[start : start + BATCH]))
            except Exception as exc:
                # A failed request says nothing about who is live, so keep the last answer.
                self.last_error = repr(exc)
                log.warning("streams.fetch_failed", error=repr(exc))
                return []
        self.last_error = None
        return await self._apply(found, wanted)

    async def _apply(
        self, found: dict[str, dict[str, Any]], wanted: Sequence[str]
    ) -> list[StreamStatusChanged]:
        at = now_ms()
        changes: list[StreamStatusChanged] = []
        for channel_id, info in found.items():
            if channel_id not in self.status.streams:
                changes.append(StreamStatusChanged(channel_id, True, at))
            self.status.streams[channel_id] = info
        for channel_id in [c for c in self.status.streams if c not in found]:
            del self.status.streams[channel_id]
            if channel_id in wanted:  # a channel we parted isn't "offline", it's gone
                changes.append(StreamStatusChanged(channel_id, False, at))
        for change in changes:
            log.info("streams.changed", channel=change.channel_id, live=change.live)
            if self.sink is not None:
                await self.sink(change)
        return changes
