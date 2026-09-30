"""Fetching history from logs.ivr.fi (ADR-0008).

The provider returns raw IRC lines; mapping them to domain events lives in `backfill.py`. Another
justlog-compatible deployment can stand in (`IVR_LOGS_URL`), and a different service can replace this by
implementing `HistoryProvider`.

The service publishes no terms or limits, so the bot keeps its own (ADR-0008): one request every
`MIN_INTERVAL_S`, at most `DAILY_BUDGET` a day (UTC), and a growing wait after a failure. After
`MAX_FAILURES` failures in a row, or once the day's budget is spent, it asks nothing more until the next day
and says so (`HistoryResponse.retry_at_ms`), so the job can wait for it.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import aiohttp
import structlog

log = structlog.get_logger(__name__)

DEFAULT_LIMIT = 1000
TIMEOUT_S = 30.0
MIN_INTERVAL_S = 10.0
DAILY_BUDGET = 200
MAX_FAILURES = 3
BACKOFF_S = (30.0, 120.0)  # waits after the first and second failure in a row
CHANNELS_TTL_S = 6 * 3600

NOT_LOGGED = "channel_not_logged"  # the service doesn't log this channel
REQUEST_FAILED = "request_failed"
PAUSED = "paused"  # the day's budget is spent, or the service kept failing: try again at `retry_at_ms`


@dataclass(frozen=True, slots=True)
class HistoryResponse:
    lines: tuple[str, ...] = ()
    error_code: str = ""
    hit_limit: bool = False
    retry_at_ms: int | None = None  # with PAUSED: when the provider will ask again

    @property
    def ok(self) -> bool:
        return not self.error_code


class HistoryProvider(Protocol):
    async def fetch(
        self, channel_id: str, *, from_ms: int, to_ms: int, limit: int, offset: int = 0
    ) -> HistoryResponse:
        """Lines sent from `from_ms` (inclusive) to `to_ms` (exclusive), oldest first: `limit` of them,
        skipping the first `offset`."""
        ...


def _rfc3339(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _next_utc_day_ms(now_s: float) -> int:
    today = datetime.fromtimestamp(now_s, UTC).date()
    tomorrow = datetime(today.year, today.month, today.day, tzinfo=UTC) + timedelta(days=1)
    return int(tomorrow.timestamp() * 1000)


class _Failed(Exception):
    """A 429, a 5xx or network trouble: worth asking again after a wait."""


@dataclass
class IvrLogsProvider:
    """logs.ivr.fi, or any justlog deployment (`IVR_LOGS_URL`)."""

    base_url: str = "https://logs.ivr.fi"
    min_interval_s: float = MIN_INTERVAL_S
    daily_budget: int = DAILY_BUDGET
    backoff_s: tuple[float, ...] = BACKOFF_S
    _session: aiohttp.ClientSession | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _last_request: float = field(default=float("-inf"), repr=False)
    _day: str = field(default="", repr=False)
    _spent: int = field(default=0, repr=False)
    _paused_until_ms: int = field(default=0, repr=False)
    _channels: frozenset[str] | None = field(default=None, repr=False)
    _channels_at: float = field(default=0.0, repr=False)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
                headers={"User-Agent": "doomtp-bot (+https://github.com/vEXOULZ/doomtp-bot)"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def fetch(
        self,
        channel_id: str,
        *,
        from_ms: int,
        to_ms: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> HistoryResponse:
        async with self._lock:  # one request at a time, however many callers
            paused = self._paused()
            if paused is not None:
                return paused
            try:
                if channel_id not in await self._logged_channels():
                    return HistoryResponse(error_code=NOT_LOGGED)
                params = {
                    "from": _rfc3339(from_ms),
                    "to": _rfc3339(to_ms),
                    "raw": "true",
                    # The service's `limit` is where the page ends, not its length: it answers lines
                    # `offset` to `limit`, so `limit=1000&offset=1000` is an empty page (ADR-0008).
                    "limit": str(offset + limit),
                    "offset": str(offset),
                }
                status, body = await self._get(f"/channelid/{channel_id}", params)
            except _Failed:
                return self._paused() or HistoryResponse(error_code=REQUEST_FAILED)
            if status == 404:  # nothing in the range, or `offset` is past its end
                return HistoryResponse()
            if status != 200:
                log.warning("history.fetch_refused", channel=channel_id, status=status, body=body[:200])
                return HistoryResponse(error_code=f"http_{status}")
            lines = tuple(line for line in body.splitlines() if line.strip())
            return HistoryResponse(lines, hit_limit=len(lines) >= limit)

    async def _logged_channels(self) -> frozenset[str]:
        """The channel ids the service logs; a 404 for one it doesn't would look like a quiet chat."""
        if self._channels is None or time.monotonic() - self._channels_at > CHANNELS_TTL_S:
            status, body = await self._get("/channels", {})
            if status != 200:
                raise _Failed(f"/channels answered {status}")
            payload = json.loads(body)
            self._channels = frozenset(str(c.get("userID")) for c in payload.get("channels", ()))
            self._channels_at = time.monotonic()
        return self._channels

    async def _get(self, path: str, params: dict[str, str]) -> tuple[int, str]:
        """One request, waited for and counted; retried after a wait while it fails, up to `MAX_FAILURES`."""
        session = await self._get_session()
        url = self.base_url.rstrip("/") + path
        for attempt in range(MAX_FAILURES):
            if not self._take_budget():
                raise _Failed("the day's budget is spent")
            wait = self._last_request + self.min_interval_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            try:
                async with session.get(url, params=params) as response:
                    status, body = response.status, await response.text()
            except Exception as exc:  # network trouble is normal; the gap simply stays open
                log.warning("history.fetch_failed", path=path, attempt=attempt + 1, error=repr(exc))
            else:
                if status != 429 and status < 500:
                    return status, body
                log.warning("history.fetch_failed", path=path, attempt=attempt + 1, status=status)
            if attempt < len(self.backoff_s) and attempt + 1 < MAX_FAILURES:
                await asyncio.sleep(self.backoff_s[attempt])
        self._paused_until_ms = _next_utc_day_ms(time.time())
        log.warning("history.paused", reason="failures", until=self._paused_until_ms)
        raise _Failed("failed too often")

    def _take_budget(self) -> bool:
        day = datetime.now(UTC).date().isoformat()
        if day != self._day:
            self._day, self._spent = day, 0
        if self._spent >= self.daily_budget:
            self._paused_until_ms = _next_utc_day_ms(time.time())
            log.warning("history.paused", reason="budget", until=self._paused_until_ms)
            return False
        self._spent += 1
        return True

    def _paused(self) -> HistoryResponse | None:
        if self._paused_until_ms > time.time() * 1000:
            return HistoryResponse(error_code=PAUSED, retry_at_ms=self._paused_until_ms)
        return None
