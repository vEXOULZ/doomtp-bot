"""Filling coverage gaps in the chat log (ADR-0008).

`log_sessions` says when the bot was listening. Anything between the end of one session and the start of
the next is a gap, and history is fetched to fill it. **Backfilled messages never reach commands,
listeners, triggers or variables** — they go to the log only, marked `source='recent-messages'`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.clock import now_ms
from doomtp_bot.core import metrics
from doomtp_bot.core.events import Badge, ChatCleared, ChatMessage, ChatNotification, MessageDeleted
from doomtp_bot.core.events import UserMessagesCleared as UserCleared
from doomtp_bot.history.irc_parse import IrcLine, badges, parse_line
from doomtp_bot.history.provider import DEFAULT_LIMIT, KEEP_WARM_LIMIT, HistoryProvider
from doomtp_bot.storage.db import Connection, transaction

if TYPE_CHECKING:
    from doomtp_bot.policy.service import PolicyService

log = structlog.get_logger(__name__)

GAP_GRACE_MS = 5_000  # ADR-0008: ask from 5s before the gap, so nothing falls between the cracks
MIN_GAP_MS = 5_000  # shorter interruptions aren't worth a request
KEEP_WARM_EVERY_S = 30 * 60


@dataclass(frozen=True, slots=True)
class Gap:
    channel_id: str
    channel_login: str
    from_ms: int
    to_ms: int

    @property
    def length_ms(self) -> int:
        return self.to_ms - self.from_ms


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    gap: Gap
    fetched: int = 0
    inserted: int = 0
    complete: bool = True
    error: str = ""


def gaps_between(sessions: Sequence[tuple[int, int | None]]) -> list[tuple[int, int]]:
    """`(from, to)` between each session's end and the next one's start, for sessions ordered by start.

    The one rule both the bot's backfill and `scripts/coverage.py` go by.
    """
    found: list[tuple[int, int]] = []
    for (_, ended_at), (next_start, _) in zip(sessions, sessions[1:], strict=False):
        if ended_at is None:
            continue  # an unclosed session is closed at startup (chatlog.close_stale_sessions)
        if next_start - int(ended_at) >= MIN_GAP_MS:
            found.append((int(ended_at), next_start))
    return found


async def find_gaps(conn: Connection, channel_id: str, channel_login: str) -> list[Gap]:
    """Coverage gaps for one channel: between each session's end and the next session's start."""
    async with await conn.execute(
        "SELECT started_at, ended_at FROM log_sessions WHERE channel_id = %s ORDER BY started_at",
        (channel_id,),
    ) as cur:
        sessions = [(int(r["started_at"]), r["ended_at"]) for r in await cur.fetchall()]
    return [Gap(channel_id, channel_login, start, end) for start, end in gaps_between(sessions)]


def to_events(
    line: IrcLine, channel_id: str, channel_login: str, raw: str
) -> ChatMessage | ChatNotification | MessageDeleted | UserCleared | ChatCleared | None:
    """One IRC line → the domain event the chat log already knows how to store (ADR-0008)."""
    sent_at = line.tag_int("tmi-sent-ts") or line.tag_int("rm-received-ts") or now_ms()
    if line.command == "PRIVMSG":
        return ChatMessage(
            message_id=line.tag("id"),
            channel_id=channel_id,
            channel_login=channel_login,
            user_id=line.tag("user-id"),
            user_login=line.nick,
            display_name=line.tag("display-name") or line.nick,
            text=line.text,
            sent_at=sent_at,
            received_at=line.tag_int("rm-received-ts") or sent_at,
            badges=tuple(Badge(set_id, version) for set_id, version in badges(line)),
            bits=line.tag_int("bits"),
            reply_parent_id=line.tag("reply-parent-msg-id") or None,
            reply_parent_login=line.tag("reply-parent-user-login") or None,
            reply_parent_display=line.tag("reply-parent-display-name") or None,
            source="recent-messages",
            raw=raw,
        )
    if line.command == "CLEARMSG":
        return MessageDeleted(
            channel_id=channel_id,
            message_id=line.tag("target-msg-id"),
            target_user_id=line.tag("target-user-id") or line.tag("login"),
            at=sent_at,
            source="recent-messages",
        )
    if line.command == "CLEARCHAT":
        target = line.params[1] if len(line.params) > 1 else ""
        if target:
            return UserCleared(channel_id, line.tag("target-user-id") or target, sent_at, "recent-messages")
        return ChatCleared(channel_id, sent_at, "recent-messages")
    if line.command == "USERNOTICE":
        return ChatNotification(
            id=line.tag("id"),
            channel_id=channel_id,
            user_id=line.tag("user-id") or None,
            type=line.tag("msg-id") or "usernotice",
            payload={k: v for k, v in line.tags.items() if k.startswith("msg-param") or k == "system-msg"},
            sent_at=sent_at,
            source="recent-messages",
        )
    return None


class BackfillService:
    """Finds gaps, fetches history for them, writes it to the log and records the attempt."""

    def __init__(
        self,
        *,
        conn: Connection,
        writer: ChatLogWriter,
        provider: HistoryProvider,
        policy: PolicyService,
    ) -> None:
        self.conn = conn
        self.writer = writer
        self.provider = provider
        self.policy = policy
        self._warm_task: asyncio.Task[None] | None = None

    def enabled_channels(self) -> list[tuple[str, str]]:
        """(channel_id, login) for channels that opted in (ADR-0008 consent)."""
        return [
            (settings.channel_id, settings.login)
            for settings in self.policy.channels()
            if settings.history_backfill and settings.active
        ]

    async def run_for_channel(self, channel_id: str, channel_login: str) -> list[BackfillOutcome]:
        filled = await self._filled_gaps(channel_id)
        return [
            await self.fill(gap)
            for gap in await find_gaps(self.conn, channel_id, channel_login)
            if (gap.from_ms, gap.to_ms) not in filled
        ]

    async def run_all(self) -> list[BackfillOutcome]:
        outcomes: list[BackfillOutcome] = []
        for channel_id, login in self.enabled_channels():
            outcomes.extend(await self.run_for_channel(channel_id, login))
        return outcomes

    async def fill(self, gap: Gap) -> BackfillOutcome:
        response = await self.provider.fetch(
            gap.channel_login, after_ms=max(0, gap.from_ms - GAP_GRACE_MS), limit=DEFAULT_LIMIT
        )
        if not response.ok:
            outcome = BackfillOutcome(gap, complete=False, error=response.error_code)
            await self._record(outcome)
            return outcome

        fetched = inserted = 0
        oldest: int | None = None
        for raw in response.lines:
            line = parse_line(raw)
            if line is None:
                continue
            fetched += 1
            event = to_events(line, gap.channel_id, gap.channel_login, raw)
            if event is None:
                continue
            at = line.tag_int("rm-received-ts") or line.tag_int("tmi-sent-ts")
            oldest = at if oldest is None else min(oldest, at)
            if at and at > gap.to_ms:
                continue  # the live session already has it
            await self._store(event)
            inserted += 1

        # Complete only if the service reached back past the gap and didn't hit its cap (ADR-0008).
        complete = not response.hit_limit and (oldest is None or oldest <= gap.from_ms)
        outcome = BackfillOutcome(gap, fetched, inserted, complete)
        await self._record(outcome)
        metrics.BACKFILL_INSERTED.inc(inserted)
        log.info(
            "history.backfilled",
            channel=gap.channel_login,
            gap_ms=gap.length_ms,
            fetched=fetched,
            inserted=inserted,
            complete=complete,
        )
        return outcome

    async def _store(self, event: object) -> None:
        match event:
            case ChatMessage():
                await self.writer.message(event)
            case ChatNotification():
                await self.writer.notification(event)
            case MessageDeleted() | UserCleared() | ChatCleared():
                await self.writer.moderation(event)

    async def _filled_gaps(self, channel_id: str) -> set[tuple[int, int]]:
        """Gaps some run has already filled completely, in one query rather than one per gap."""
        async with await self.conn.execute(
            "SELECT gap_from, gap_to FROM backfill_runs WHERE channel_id = %s"
            " GROUP BY gap_from, gap_to HAVING bool_or(complete)",
            (channel_id,),
        ) as cur:
            return {(int(r["gap_from"]), int(r["gap_to"])) for r in await cur.fetchall()}

    async def _record(self, outcome: BackfillOutcome) -> None:
        if not outcome.complete:
            metrics.BACKFILL_INCOMPLETE.inc()
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO backfill_runs (channel_id, gap_from, gap_to, fetched, inserted, complete,"
                " error, at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (outcome.gap.channel_id, outcome.gap.from_ms, outcome.gap.to_ms, outcome.fetched,
                 outcome.inserted, outcome.complete, outcome.error or None, now_ms()),
            )  # fmt: skip

    # ── keep warm (ADR-0008): the service only collects channels it's asked about ──
    def start_keep_warm(self, every_s: float = KEEP_WARM_EVERY_S) -> None:
        if self._warm_task is None:
            self._warm_task = asyncio.create_task(self._keep_warm_loop(every_s), name="history-keep-warm")

    async def stop(self) -> None:
        if self._warm_task is not None:
            self._warm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._warm_task
            self._warm_task = None

    async def keep_warm_once(self) -> int:
        channels = self.enabled_channels()
        for _, login in channels:
            await self.provider.fetch(login, after_ms=None, limit=KEEP_WARM_LIMIT)
        return len(channels)

    async def _keep_warm_loop(self, every_s: float) -> None:
        while True:
            await asyncio.sleep(every_s)
            try:
                await self.keep_warm_once()
            except Exception:
                log.exception("history.keep_warm_failed")
