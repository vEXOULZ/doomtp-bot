"""Outbox: the only path to Twitch chat (architecture §2, §8; ADR-0001).

Per channel: optional reply hold → moderation recheck → filter hook → chunking → token bucket → send → log.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)

MAX_CHUNK = 500
MAX_CHUNKS = 2
DEFAULT_TTL_S = 15.0
# Twitch rejects identical consecutive messages; this invisible tag character is what chat clients use as a workaround.
DUPLICATE_SUFFIX = " \U000e0000"
# Twitch answered the send with 403: the bot may not talk in that channel, which means it was banned
# there. The sender reports it with this reason, and the outbox hands the channel to `on_banned`.
BANNED = "banned"


@dataclass(frozen=True, slots=True)
class SendResult:
    message_id: str | None
    dropped_reason: str | None = None


class ChatSender(Protocol):
    async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult: ...


class OutboundLog(Protocol):
    async def outbound(
        self,
        *,
        channel_id: str,
        text_sent: str | None,
        text_prefilter: str | None,
        twitch_message_id: str | None,
        dropped_reason: str | None,
        filter_hits: tuple[str, ...] | list[str] = (),
        run_ref: str | None = None,
    ) -> None: ...


FilterFn = Callable[
    [str, str], tuple[str | None, list[str]]
]  # (channel_id, text) → (text or None=block, hits)


def passthrough_filter(channel_id: str, text: str) -> tuple[str | None, list[str]]:
    return text, []


class TokenBucket:
    def __init__(
        self, capacity: int, per_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.capacity = capacity
        self.rate = capacity / per_seconds
        self.tokens = float(capacity)
        self.clock = clock
        self.updated = clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def wait_time(self) -> float:
        self._refill()
        return 0.0 if self.tokens >= 1 else (1 - self.tokens) / self.rate

    def take(self) -> None:
        self._refill()
        self.tokens -= 1


def chunk(text: str, size: int = MAX_CHUNK, limit: int = MAX_CHUNKS) -> list[str]:
    """Split on whitespace into ≤size chunks; the last kept chunk ends with … if text was cut."""
    words = " ".join(text.split()).split(" ")
    chunks: list[str] = []
    current = ""
    for word in words:
        while len(word) > size:  # a single overlong word
            if current:
                chunks.append(current)
                current = ""
            chunks.append(word[:size])
            word = word[size:]
        candidate = f"{current} {word}" if current else word
        if len(candidate) > size:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    if len(chunks) > limit:
        chunks = chunks[:limit]
        chunks[-1] = chunks[-1][: size - 1].rstrip() + "…"
    return chunks


class Outbox:
    def __init__(
        self,
        sender: ChatSender,
        outbound_log: OutboundLog | None = None,
        *,
        rate_for: Callable[[str], tuple[int, float]] = lambda channel_id: (20, 30.0),
        hold_ms_for: Callable[[str], int] = lambda channel_id: 0,
        content_filter: FilterFn = passthrough_filter,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_banned: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.sender = sender
        self.on_banned = on_banned
        self.outbound_log = outbound_log
        self.rate_for = rate_for
        self.hold_ms_for = hold_ms_for
        self.content_filter = content_filter
        self.ttl_s = ttl_s
        self.clock = clock
        self.sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_text: dict[str, str] = {}
        self.dropped: dict[str, int] = {}

    def _bucket(self, channel_id: str) -> TokenBucket:
        capacity, per = self.rate_for(channel_id)
        bucket = self._buckets.get(channel_id)
        if bucket is None or bucket.capacity != capacity:
            bucket = self._buckets[channel_id] = TokenBucket(capacity, per, self.clock)
        return bucket

    async def send(
        self,
        channel_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        is_invalidated: Callable[[], bool] = lambda: False,
        run_ref: str | None = None,
    ) -> list[SendResult]:
        created = self.clock()
        hold = self.hold_ms_for(channel_id)
        if hold > 0:
            await self.sleep(hold / 1000)

        filtered, hits = self.content_filter(channel_id, text)
        if filtered is None:
            await self._drop(channel_id, text, "filter_block", hits, run_ref)
            return [SendResult(None, "filter_block")]

        results: list[SendResult] = []
        lock = self._locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            for part in chunk(filtered):
                bucket = self._bucket(channel_id)
                while (wait := bucket.wait_time()) > 0:
                    if self.clock() + wait - created > self.ttl_s:
                        await self._drop(channel_id, text, "ttl", hits, run_ref)
                        results.append(SendResult(None, "ttl"))
                        return results
                    await self.sleep(wait)
                if is_invalidated():  # recheck immediately before the Helix call (spec §6.7)
                    await self._drop(channel_id, text, "moderated", hits, run_ref)
                    results.append(SendResult(None, "moderated"))
                    return results
                if self._last_text.get(channel_id) == part:
                    part += DUPLICATE_SUFFIX
                bucket.take()
                try:
                    result = await self.sender.send_chat(channel_id, part, reply_to)
                except Exception as exc:
                    log.warning("outbox.send_failed", channel=channel_id, error=repr(exc))
                    result = SendResult(None, "send_error")
                if result.dropped_reason is None:
                    self._last_text[channel_id] = part
                else:
                    self.dropped[result.dropped_reason] = self.dropped.get(result.dropped_reason, 0) + 1
                if self.outbound_log is not None:
                    await self.outbound_log.outbound(
                        channel_id=channel_id,
                        text_sent=part if result.dropped_reason is None else None,
                        text_prefilter=text,
                        twitch_message_id=result.message_id,
                        dropped_reason=result.dropped_reason,
                        filter_hits=hits,
                        run_ref=run_ref,
                    )
                results.append(result)
                if result.dropped_reason == BANNED:
                    await self._banned(channel_id)
                    return results  # the rest of the message would only be refused the same way
                reply_to = None  # only the first chunk is threaded as a reply
        return results

    async def _banned(self, channel_id: str) -> None:
        """Leave rather than keep talking into a channel that banned the bot (architecture §10)."""
        log.warning("outbox.banned", channel=channel_id)
        if self.on_banned is None:
            return
        try:
            await self.on_banned(channel_id)
        except Exception:  # leaving is best-effort; the next refused send tries again
            log.exception("outbox.leave_failed", channel=channel_id)

    async def _drop(
        self, channel_id: str, text: str, reason: str, hits: list[str], run_ref: str | None = None
    ) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1
        log.info("outbox.dropped", channel=channel_id, reason=reason)
        if self.outbound_log is not None:
            await self.outbound_log.outbound(
                channel_id=channel_id,
                text_sent=None,
                text_prefilter=text,
                twitch_message_id=None,
                dropped_reason=reason,
                filter_hits=hits,
            )
