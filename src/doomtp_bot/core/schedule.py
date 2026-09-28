"""`$channel.next_stream`: each channel's stream schedule, fetched when a command first asks (ADR-0019).

A schedule changes rarely and is read only by the few commands that name it, so unlike the live set
(`core/streams.py`) nothing polls it: the first read in a channel asks Helix, and the answer is kept for
`TTL_S`. A failed request is kept for less, so a Twitch outage costs one request a minute per channel
at most, and the readout says nothing is scheduled rather than failing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)

TTL_S = 600.0
FAILED_TTL_S = 60.0


class ScheduleSource(Protocol):
    async def fetch_schedule(self, channel_id: str) -> list[dict[str, str]]:
        """Upcoming streams as {title, category, start (ISO 8601)}, soonest first. Raises on failure."""
        ...


class NextStreams:
    def __init__(
        self,
        source: ScheduleSource,
        *,
        ttl_s: float = TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self.ttl_s = ttl_s
        self.clock = clock
        self._cache: dict[str, tuple[float, list[dict[str, str]]]] = {}

    async def upcoming(self, channel_id: str) -> list[dict[str, str]]:
        hit = self._cache.get(channel_id)
        if hit is not None and hit[0] > self.clock():
            return hit[1]
        try:
            found, ttl = await self.source.fetch_schedule(channel_id), self.ttl_s
        except Exception as exc:
            log.warning("schedule.fetch_failed", channel=channel_id, error=repr(exc))
            found, ttl = [], FAILED_TTL_S
        self._cache[channel_id] = (self.clock() + ttl, found)
        return found

    async def next_stream(self, channel_id: str, now: float) -> dict[str, str | int] | None:
        """The first stream that hasn't started by `now` (unix seconds), with `in` as seconds from now.
        An empty title or category is left out, so a readout's `??` wording shows instead."""
        for segment in await self.upcoming(channel_id):
            start = datetime.fromisoformat(segment["start"]).timestamp()
            if start > now:
                fields: dict[str, str | int] = {k: v for k, v in segment.items() if v}
                fields["in"] = int(start - now)
                return fields
        return None
