"""Filling coverage gaps in the chat log (ADR-0008).

`log_sessions` says when the bot was listening. Anything between the end of one session and the start of
the next is a gap, and history is fetched to fill it. **Backfilled messages never reach commands,
listeners, triggers or variables** — they go to the log only, marked `source='ivr-logs'`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

import structlog

from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.clock import now_ms
from doomtp_bot.core import metrics
from doomtp_bot.core.events import Badge, ChatCleared, ChatMessage, ChatNotification, MessageDeleted
from doomtp_bot.core.events import UserMessagesCleared as UserCleared
from doomtp_bot.history import irc_convert
from doomtp_bot.history.enrich import Enricher
from doomtp_bot.history.irc_parse import IrcLine, badges, parse_line
from doomtp_bot.history.provider import DEFAULT_LIMIT, PAUSED, HistoryProvider
from doomtp_bot.storage.db import Connection, transaction

if TYPE_CHECKING:
    from doomtp_bot.policy.service import PolicyService

log = structlog.get_logger(__name__)

GAP_GRACE_MS = 5_000  # ADR-0008: ask from 5s before the gap, so nothing falls between the cracks
MIN_GAP_MS = 5_000  # shorter interruptions aren't worth a request
PROVIDER: Final = "ivr-logs"  # `backfill_runs.provider`, and the `source` of what it brings in
STOPPED: Final = "stopped"  # the fill was asked to stop: a cancel, or the bot shutting down


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
    reached_ms: int | None = None  # the newest line stored: where the next fill of the gap resumes


@dataclass(frozen=True, slots=True)
class FillResult:
    """One fill over several gaps: an outcome per gap, and what the requests brought in all."""

    outcomes: tuple[BackfillOutcome, ...]
    fetched: int = 0
    inserted: int = 0
    error: str = ""
    retry_at_ms: int | None = None  # the provider paused: the job waits until then

    @property
    def complete(self) -> bool:
        return all(o.complete for o in self.outcomes)

    @property
    def stopped(self) -> bool:
        return self.error == STOPPED


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
    """One IRC line → the domain event the chat log already knows how to store (ADR-0008), with its fields
    in EventSub's terms (`irc_convert`)."""
    sent_at = line.tag_int("tmi-sent-ts") or now_ms()
    if line.command == "PRIVMSG":
        info = irc_convert.badge_info(line)
        return ChatMessage(
            message_id=line.tag("id"),
            channel_id=channel_id,
            channel_login=channel_login,
            user_id=line.tag("user-id"),
            user_login=line.nick,
            display_name=line.tag("display-name") or line.nick,
            text=irc_convert.text(line),
            sent_at=sent_at,
            received_at=sent_at,  # the service keeps only when Twitch sent it
            badges=tuple(Badge(set_id, version, info.get(set_id, "")) for set_id, version in badges(line)),
            fragments=irc_convert.fragments(line),
            message_type=irc_convert.message_type(line),
            bits=line.tag_int("bits"),
            reply_parent_id=line.tag("reply-parent-msg-id") or None,
            reply_parent_user_id=line.tag("reply-parent-user-id") or None,
            reply_parent_login=line.tag("reply-parent-user-login") or None,
            reply_parent_display=line.tag("reply-parent-display-name") or None,
            reward_id=line.tag("custom-reward-id") or None,
            source_channel_id=line.tag("source-room-id") or None,
            source=PROVIDER,
            raw_line=raw,
        )
    if line.command == "CLEARMSG":
        return MessageDeleted(
            channel_id=channel_id,
            message_id=line.tag("target-msg-id"),
            # Twitch's CLEARMSG names the author by login only; `fill_many` finds their id.
            target_user_id=line.tag("target-user-id") or None,
            at=sent_at,
            source=PROVIDER,
            raw_line=raw,
        )
    if line.command == "CLEARCHAT":
        target = line.params[1] if len(line.params) > 1 else ""
        if target:
            ban = line.tags.get("ban-duration")  # a timeout's length; a ban has none
            return UserCleared(
                channel_id, line.tag("target-user-id") or target, sent_at, PROVIDER, raw_line=raw,
                duration_s=int(ban) if ban and ban.isdigit() else None,
            )  # fmt: skip
        return ChatCleared(channel_id, sent_at, PROVIDER, raw_line=raw)
    if line.command == "USERNOTICE":
        return ChatNotification(
            id=line.tag("id"),
            channel_id=channel_id,
            user_id=irc_convert.notice_user_id(line),
            type=irc_convert.notice_type(line),
            payload=irc_convert.notice_payload(line),
            sent_at=sent_at,
            source=PROVIDER,
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
        enricher: Enricher | None = None,
    ) -> None:
        self.conn = conn
        self.writer = writer
        self.provider = provider
        self.policy = policy
        self.enricher = enricher

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
        """Coverage gaps no run has filled completely. A `chat_backfill` job fills them, one per channel
        (ADR-0024 §5, ADR-0027)."""
        filled = await self._filled_gaps(channel_id)
        return [
            gap
            for gap in await find_gaps(self.conn, channel_id, channel_login)
            if (gap.from_ms, gap.to_ms) not in filled
        ]

    async def fill(self, gap: Gap) -> BackfillOutcome:
        (outcome,) = (await self.fill_many([gap])).outcomes
        return outcome

    async def fill_many(
        self,
        gaps: Sequence[Gap],
        *,
        progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], Awaitable[bool]] | None = None,
    ) -> FillResult:
        """Fill one channel's gaps, oldest first (ADR-0008, ADR-0024 §5).

        Each gap is asked for on its own, from 5s before it starts to its end, and paged through oldest
        first: gaps can lie months apart, and the chat between them is in the live log already. A gap is
        complete once a page comes back short. When the provider stops a fill (a failure, or its daily
        budget), the gap is recorded incomplete with the newest line stored, and the next fill resumes
        there rather than asking for the same lines again. The gaps after it wait for that fill too.

        `progress(done, total)` is told each time a gap is done with. `should_stop()` is asked before each
        request: once it says so, the fill stops as a failure would, with error `STOPPED` (ADR-0027).
        """
        ordered = sorted(gaps, key=lambda g: g.from_ms)
        if not ordered:
            return FillResult(())
        channel_id, login = ordered[0].channel_id, ordered[0].channel_login
        resume = await self._resume_points(channel_id)
        enriching = None if self.enricher is None else self.enricher.fill(channel_id)
        outcomes: list[BackfillOutcome] = []
        authors: dict[str, str] = {}  # message id → user id, for the deletes that name only a login
        fetched = inserted = requests = 0
        error = ""
        retry_at: int | None = None
        if progress is not None:
            progress(0, len(ordered))
        for gap in ordered:
            reached = resume.get((gap.from_ms, gap.to_ms))
            if error:
                outcomes.append(BackfillOutcome(gap, complete=False, error=error, reached_ms=reached))
                if progress is not None:
                    progress(len(outcomes), len(ordered))
                continue
            start = max(gap.from_ms - GAP_GRACE_MS, 0)
            if reached is not None:
                start = max(start, reached)  # inclusive: lines at `reached` itself are stored once
            count = stored = offset = 0
            complete = False
            while True:
                if should_stop is not None and await should_stop():
                    error = STOPPED
                    break
                response = await self.provider.fetch(
                    channel_id, from_ms=start, to_ms=gap.to_ms + 1, limit=DEFAULT_LIMIT, offset=offset
                )
                requests += 1
                if not response.ok:
                    error = response.error_code
                    if error == PAUSED:
                        retry_at = response.retry_at_ms
                    break
                offset += len(response.lines)
                for raw in response.lines:
                    line = parse_line(raw)
                    if line is None:
                        continue
                    count += 1
                    event = to_events(line, channel_id, login, raw)
                    if event is None:
                        continue
                    if isinstance(event, ChatMessage):
                        authors[event.message_id] = event.user_id
                        if enriching is not None:  # what the line lacks (ADR-0024 §3)
                            event = replace(event, enrichment=await enriching.message(event))
                    elif isinstance(event, MessageDeleted) and event.target_user_id is None:
                        author = authors.get(event.message_id) or await self._author(event.message_id)
                        event = replace(event, target_user_id=author)
                    await self._store(event)
                    stored += 1
                    if at := line.tag_int("tmi-sent-ts"):
                        reached = at if reached is None else max(reached, at)
                if not response.hit_limit:
                    complete = True
                    break
            fetched += count
            inserted += stored
            outcomes.append(BackfillOutcome(gap, count, stored, complete, "" if complete else error, reached))
            if progress is not None:
                progress(len(outcomes), len(ordered))

        for outcome in outcomes:
            await self._record(outcome)
        metrics.BACKFILL_INSERTED.inc(inserted)
        log.info(
            "history.backfilled",
            channel=login,
            gaps=len(outcomes),
            requests=requests,
            fetched=fetched,
            inserted=inserted,
            complete=sum(o.complete for o in outcomes),
            error=error or None,
        )
        return FillResult(tuple(outcomes), fetched, inserted, error, retry_at)

    async def _store(self, event: object) -> None:
        match event:
            case ChatMessage():
                await self.writer.message(event)
            case ChatNotification():
                await self.writer.notification(event)
            case MessageDeleted() | UserCleared() | ChatCleared():
                await self.writer.moderation(event)

    async def _author(self, message_id: str) -> str | None:
        """Who sent a message the log already has, for a delete that came before this fill."""
        async with await self.conn.execute(
            "SELECT user_id FROM messages WHERE message_id = %s", (message_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else str(row["user_id"])

    async def _filled_gaps(self, channel_id: str) -> set[tuple[int, int]]:
        """Gaps some run has filled completely, in one query rather than one per gap."""
        async with await self.conn.execute(
            "SELECT gap_from, gap_to FROM backfill_runs WHERE channel_id = %s"
            " GROUP BY gap_from, gap_to HAVING bool_or(complete)",
            (channel_id,),
        ) as cur:
            return {(int(r["gap_from"]), int(r["gap_to"])) for r in await cur.fetchall()}

    async def _resume_points(self, channel_id: str) -> dict[tuple[int, int], int]:
        """For each gap a fill was stopped in, the newest line stored for it."""
        async with await self.conn.execute(
            "SELECT gap_from, gap_to, max(reached_ms) AS reached FROM backfill_runs"
            " WHERE channel_id = %s AND provider = %s AND reached_ms IS NOT NULL GROUP BY gap_from, gap_to",
            (channel_id, PROVIDER),
        ) as cur:
            return {(int(r["gap_from"]), int(r["gap_to"])): int(r["reached"]) for r in await cur.fetchall()}

    async def _record(self, outcome: BackfillOutcome) -> None:
        if not outcome.complete:
            metrics.BACKFILL_INCOMPLETE.inc()
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO backfill_runs (channel_id, gap_from, gap_to, fetched, inserted, complete,"
                " error, provider, reached_ms, at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (outcome.gap.channel_id, outcome.gap.from_ms, outcome.gap.to_ms, outcome.fetched,
                 outcome.inserted, outcome.complete, outcome.error or None, PROVIDER, outcome.reached_ms,
                 now_ms()),
            )  # fmt: skip
