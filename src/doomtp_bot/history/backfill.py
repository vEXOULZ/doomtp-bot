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
MAX_PAGES = 20  # requests one fill may make; what's left stays open for the next job
OUT_OF_REACH = "out_of_reach"  # the service's history starts after the gap did: asking again can't help
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


@dataclass(frozen=True, slots=True)
class FillResult:
    """One fill over several gaps: an outcome per gap, and what the requests brought in all."""

    outcomes: tuple[BackfillOutcome, ...]
    fetched: int = 0
    inserted: int = 0
    error: str = ""

    @property
    def complete(self) -> bool:
        return all(o.complete for o in self.outcomes)


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
            raw_line=raw,
        )
    if line.command == "CLEARMSG":
        return MessageDeleted(
            channel_id=channel_id,
            message_id=line.tag("target-msg-id"),
            target_user_id=line.tag("target-user-id") or line.tag("login"),
            at=sent_at,
            source="recent-messages",
            raw_line=raw,
        )
    if line.command == "CLEARCHAT":
        target = line.params[1] if len(line.params) > 1 else ""
        if target:
            return UserCleared(
                channel_id, line.tag("target-user-id") or target, sent_at, "recent-messages", raw_line=raw
            )
        return ChatCleared(channel_id, sent_at, "recent-messages", raw_line=raw)
    if line.command == "USERNOTICE":
        return ChatNotification(
            id=line.tag("id"),
            channel_id=channel_id,
            user_id=line.tag("user-id") or None,
            type=line.tag("msg-id") or "usernotice",
            payload={k: v for k, v in line.tags.items() if k.startswith("msg-param") or k == "system-msg"},
            sent_at=sent_at,
            source="recent-messages",
            raw_line=raw,
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

    def login_if_enabled(self, channel_id: str) -> str | None:
        """The channel's login while it is active and opted in; None otherwise."""
        return next((login for cid, login in self.enabled_channels() if cid == channel_id), None)

    async def open_gaps(self, channel_id: str, channel_login: str) -> list[Gap]:
        """Coverage gaps no run has settled: filled completely, or found out of the service's reach.
        `BackfillQueue` fills them in one job per channel (ADR-0024 §5)."""
        filled = await self._filled_gaps(channel_id)
        return [
            gap
            for gap in await find_gaps(self.conn, channel_id, channel_login)
            if (gap.from_ms, gap.to_ms) not in filled
        ]

    async def fill(self, gap: Gap) -> BackfillOutcome:
        (outcome,) = (await self.fill_many([gap])).outcomes
        return outcome

    async def fill_many(self, gaps: Sequence[Gap]) -> FillResult:
        """Fill one channel's gaps with as few requests as the service allows (ADR-0024 §5).

        The service takes a start and no end, and answers oldest first, so one request from the oldest gap
        covers every later gap its 800 lines reach. Only lines inside a gap are stored; the live log has the
        rest. When the cap cuts a page short, the next one carries on from its newest line; when the next gap
        starts past that, it starts at the gap instead, skipping the chat in between.

        A gap is complete when history reaches back to its start and forward past its end (ADR-0008). History
        that starts after a gap did can't be made to reach further back by asking again: the gap is recorded
        `out_of_reach` and not queued again.
        """
        pending = sorted(gaps, key=lambda g: g.from_ms)
        if not pending:
            return FillResult(())
        channel_id, login = pending[0].channel_id, pending[0].channel_login
        outcomes: list[BackfillOutcome] = []
        per_gap: dict[Gap, int] = dict.fromkeys(pending, 0)
        seen: set[str] = set()
        fetched = pages = 0
        error = ""
        covered_from: int | None = None  # history is unbroken from here to the end of the last page
        chain_at: int | None = None  # the newest line of the last page, when the cap cut it short
        while pending and pages < MAX_PAGES:
            start = max(0, pending[0].from_ms - GAP_GRACE_MS)
            chained = chain_at is not None and chain_at >= start
            after = chain_at if chained and chain_at is not None else start
            response = await self.provider.fetch(login, after_ms=after, limit=DEFAULT_LIMIT)
            pages += 1
            if not response.ok:
                error = response.error_code
                break
            oldest: int | None = None
            newest: int | None = None
            for raw in response.lines:
                line = parse_line(raw)
                if line is None:
                    continue
                at = line.tag_int("rm-received-ts") or line.tag_int("tmi-sent-ts")
                if at:
                    oldest = at if oldest is None else min(oldest, at)
                    newest = at if newest is None else max(newest, at)
                if raw in seen:
                    continue  # a chained page starts with the last one's newest lines
                seen.add(raw)
                fetched += 1
                event = to_events(line, channel_id, login, raw)
                if event is None:
                    continue
                gap: Gap | None = pending[0]
                if at:
                    gap = next((g for g in pending if g.from_ms - GAP_GRACE_MS <= at <= g.to_ms), None)
                if gap is None:
                    continue  # between gaps: the live log already has it
                await self._store(event)
                per_gap[gap] += 1
            if not chained:
                covered_from = oldest
            reached = (newest or 0) if response.hit_limit else None  # None: up to now
            still: list[Gap] = []
            for gap in pending:
                if reached is not None and gap.to_ms > reached:
                    still.append(gap)
                    continue
                complete = covered_from is None or covered_from <= gap.from_ms
                count = per_gap[gap]
                outcomes.append(
                    BackfillOutcome(gap, count, count, complete, "" if complete else OUT_OF_REACH)
                )
            pending = still
            if newest is None or (chain_at is not None and newest <= chain_at):
                break  # nothing newer came back: asking again would get the same page
            chain_at = newest
        # Whatever a failure or the page limit left: incomplete, and open for the next job.
        outcomes.extend(BackfillOutcome(g, per_gap[g], per_gap[g], False, error) for g in pending)

        for outcome in outcomes:
            await self._record(outcome)
        inserted = sum(per_gap.values())
        metrics.BACKFILL_INSERTED.inc(inserted)
        log.info(
            "history.backfilled",
            channel=login,
            gaps=len(outcomes),
            requests=pages,
            fetched=fetched,
            inserted=inserted,
            complete=sum(o.complete for o in outcomes),
            out_of_reach=sum(o.error == OUT_OF_REACH for o in outcomes),
            error=error or None,
        )
        return FillResult(tuple(sorted(outcomes, key=lambda o: o.gap.from_ms)), fetched, inserted, error)

    async def _store(self, event: object) -> None:
        match event:
            case ChatMessage():
                await self.writer.message(event)
            case ChatNotification():
                await self.writer.notification(event)
            case MessageDeleted() | UserCleared() | ChatCleared():
                await self.writer.moderation(event)

    async def _filled_gaps(self, channel_id: str) -> set[tuple[int, int]]:
        """Gaps some run has settled, in one query rather than one per gap."""
        async with await self.conn.execute(
            "SELECT gap_from, gap_to FROM backfill_runs WHERE channel_id = %s"
            " GROUP BY gap_from, gap_to HAVING bool_or(complete OR error = %s)",
            (channel_id, OUT_OF_REACH),
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
