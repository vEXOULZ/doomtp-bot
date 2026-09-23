"""Fetching history from recent-messages (ADR-0008).

The provider returns raw IRC lines; mapping them to domain events lives in `backfill.py`. A different
service (or a self-hosted recent-messages2) can replace this by implementing `HistoryProvider`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import aiohttp
import structlog

log = structlog.get_logger(__name__)

DEFAULT_LIMIT = 800
KEEP_WARM_LIMIT = 1
TIMEOUT_S = 15.0


@dataclass(frozen=True, slots=True)
class HistoryResponse:
    lines: tuple[str, ...] = ()
    error_code: str = ""  # e.g. channel_not_joined, channel_ignored
    hit_limit: bool = False

    @property
    def ok(self) -> bool:
        return not self.error_code


class HistoryProvider(Protocol):
    async def fetch(self, channel_login: str, *, after_ms: int | None, limit: int) -> HistoryResponse: ...


@dataclass
class RecentMessagesProvider:
    """recent-messages.robotty.de, or any compatible deployment (`HISTORY_PROVIDER_URL`)."""

    base_url: str
    _session: aiohttp.ClientSession | None = field(default=None, repr=False)

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
        self, channel_login: str, *, after_ms: int | None = None, limit: int = DEFAULT_LIMIT
    ) -> HistoryResponse:
        url = f"{self.base_url.rstrip('/')}/recent-messages/{channel_login.lower()}"
        params: dict[str, str] = {"limit": str(limit)}
        if after_ms is not None:
            params["after"] = str(after_ms)
        session = await self._get_session()
        try:
            async with session.get(url, params=params) as response:
                payload = await response.json(content_type=None)
        except Exception as exc:  # network trouble is normal; the gap simply stays open
            log.warning("history.fetch_failed", channel=channel_login, error=repr(exc))
            return HistoryResponse(error_code="request_failed")
        error = str(payload.get("error_code") or payload.get("error") or "")
        lines = tuple(str(line) for line in payload.get("messages", ()))
        return HistoryResponse(lines, error, hit_limit=len(lines) >= limit)
