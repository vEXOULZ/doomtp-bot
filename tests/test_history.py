"""History backfill: IRC parsing, gap detection and filling (ADR-0008)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.core.events import ChatCleared, ChatMessage, ChatNotification, MessageDeleted
from doomtp_bot.core.events import UserMessagesCleared as UserCleared
from doomtp_bot.history.backfill import BackfillService, Gap, find_gaps, to_events
from doomtp_bot.history.irc_parse import badges, parse_line, unescape_tag
from doomtp_bot.history.provider import HistoryResponse
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.storage.db import Databases

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"

PRIVMSG = (
    "@badge-info=subscriber/12;badges=subscriber/12,moderator/1;color=#1E90FF;display-name=Alice;"
    "id=abc-123;user-id=400;tmi-sent-ts=1000;rm-received-ts=1005 "
    ":alice!alice@alice.tmi.twitch.tv PRIVMSG #doomtp :hello there"
)
CLEARMSG = (
    "@login=alice;target-msg-id=abc-123;target-user-id=400;tmi-sent-ts=2000 "
    ":tmi.twitch.tv CLEARMSG #doomtp :hello there"
)
CLEARCHAT_USER = (
    "@ban-duration=600;target-user-id=400;tmi-sent-ts=3000 :tmi.twitch.tv CLEARCHAT #doomtp :alice"
)
CLEARCHAT_ALL = "@tmi-sent-ts=4000 :tmi.twitch.tv CLEARCHAT #doomtp"
USERNOTICE = (
    "@msg-id=resub;msg-param-cumulative-months=12;system-msg=Alice\\ssubscribed\\sfor\\s12\\smonths;"
    "id=note-1;user-id=400;tmi-sent-ts=5000 :tmi.twitch.tv USERNOTICE #doomtp :thanks!"
)


# ── IRC parsing ────────────────────────────────────────────────────────────
def test_tags_prefix_command_and_trailing_parameter() -> None:
    line = parse_line(PRIVMSG)
    assert line is not None
    assert (line.command, line.channel, line.text, line.nick) == ("PRIVMSG", "doomtp", "hello there", "alice")
    assert line.tag("display-name") == "Alice" and line.tag_int("tmi-sent-ts") == 1000
    assert badges(line) == (("subscriber", "12"), ("moderator", "1"))


def test_a_missing_trailing_parameter_is_fine() -> None:
    line = parse_line(CLEARCHAT_ALL)
    assert line is not None and line.command == "CLEARCHAT" and line.params == ("#doomtp",)


def test_tag_order_does_not_matter() -> None:
    reordered = "@user-id=400;id=abc-123;tmi-sent-ts=1000 :alice!alice@x PRIVMSG #doomtp :hi"
    line = parse_line(reordered)
    assert line is not None and line.tag("id") == "abc-123" and line.tag("user-id") == "400"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(r"Alice\ssubscribed", "Alice subscribed"), (r"a\:b", "a;b"), (r"back\\slash", "back\\slash")],
)
def test_tag_escapes(raw: str, expected: str) -> None:
    assert unescape_tag(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "@only-tags=1"])
def test_unusable_lines_are_skipped(raw: str) -> None:
    assert parse_line(raw) is None


# ── mapping to events ──────────────────────────────────────────────────────
def test_privmsg_becomes_a_message_marked_as_history() -> None:
    line = parse_line(PRIVMSG)
    assert line is not None
    event = to_events(line, CHANNEL_ID, CHANNEL_LOGIN, PRIVMSG)
    assert isinstance(event, ChatMessage)
    assert (event.message_id, event.user_id, event.text) == ("abc-123", "400", "hello there")
    assert event.source == "recent-messages" and event.raw == PRIVMSG
    assert event.sent_at == 1000 and event.received_at == 1005


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (CLEARMSG, MessageDeleted),
        (CLEARCHAT_USER, UserCleared),
        (CLEARCHAT_ALL, ChatCleared),
        (USERNOTICE, ChatNotification),
    ],
)
def test_moderation_and_notice_lines_map_to_their_events(raw: str, kind: type) -> None:
    line = parse_line(raw)
    assert line is not None
    assert isinstance(to_events(line, CHANNEL_ID, CHANNEL_LOGIN, raw), kind)


# ── gaps ───────────────────────────────────────────────────────────────────
async def test_gaps_are_the_holes_between_sessions(dbs: Databases) -> None:
    await dbs.chatlog.executescript(
        """
        INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason)
             VALUES ('100', 1000, 2000, 'shutdown'), ('100', 9000, 9500, 'shutdown');
        INSERT INTO log_sessions (channel_id, started_at) VALUES ('100', 20000);
        INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason)
             VALUES ('200', 1000, 2000, 'shutdown');
        """
    )
    await dbs.chatlog.commit()
    gaps = await find_gaps(dbs.chatlog, CHANNEL_ID, CHANNEL_LOGIN)
    assert [(g.from_ms, g.to_ms) for g in gaps] == [(2000, 9000), (9500, 20000)]


async def test_short_interruptions_are_not_gaps(dbs: Databases) -> None:
    await dbs.chatlog.executescript(
        """
        INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason)
             VALUES ('100', 1000, 2000, 'shutdown');
        INSERT INTO log_sessions (channel_id, started_at) VALUES ('100', 2100);
        """
    )
    await dbs.chatlog.commit()
    assert await find_gaps(dbs.chatlog, CHANNEL_ID, CHANNEL_LOGIN) == []


# ── filling them ───────────────────────────────────────────────────────────
@dataclass
class FakeProvider:
    response: HistoryResponse = field(default_factory=HistoryResponse)
    calls: list[tuple[str, int | None, int]] = field(default_factory=list)

    async def fetch(
        self, channel_login: str, *, after_ms: int | None = None, limit: int = 800
    ) -> HistoryResponse:
        self.calls.append((channel_login, after_ms, limit))
        return self.response


async def backfill_for(dbs: Databases, provider: FakeProvider, *, opted_in: bool = True) -> BackfillService:
    policy = PolicyService(dbs.bot)
    await policy.reload()
    await policy.mutate(lambda repo: repo.ensure_channel(CHANNEL_ID, CHANNEL_LOGIN, Actor(None, "system")))
    await policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "history_backfill", int(opted_in), Actor(None, "s"))
    )
    writer = ChatLogWriter(dbs.chatlog)
    return BackfillService(conn=dbs.chatlog, writer=writer, provider=provider, policy=policy)


async def test_a_gap_is_filled_from_history_and_recorded(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG, CLEARMSG, USERNOTICE)))
    service = await backfill_for(dbs, provider)
    gap = Gap(CHANNEL_ID, CHANNEL_LOGIN, 1100, 6000)  # the oldest line (1005) predates the gap

    outcome = await service.fill(gap)
    await service.writer.stop()

    assert provider.calls == [(CHANNEL_LOGIN, 0, 800)]  # asked from 5s before the gap, clamped at 0
    assert (outcome.fetched, outcome.inserted, outcome.complete) == (3, 3, True)
    async with dbs.chatlog.execute("SELECT message_id, source, raw FROM messages") as cur:
        rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [("abc-123", "recent-messages", PRIVMSG)]
    async with dbs.chatlog.execute("SELECT deleted_at FROM messages WHERE message_id = 'abc-123'") as cur:
        assert (await cur.fetchone())[0] == 2000  # the CLEARMSG flagged it, without deleting the row
    async with dbs.chatlog.execute("SELECT fetched, inserted, complete FROM backfill_runs") as cur:
        assert [tuple(r) for r in await cur.fetchall()] == [(3, 3, 1)]


@pytest.mark.parametrize(
    ("response", "gap_from", "why"),
    [
        (HistoryResponse(lines=(PRIVMSG,), hit_limit=True), 900, "the service hit its cap"),
        (HistoryResponse(lines=(PRIVMSG,)), 500, "history starts after the gap did"),
    ],
)
async def test_a_gap_that_cannot_be_proven_covered_is_incomplete(
    dbs: Databases, response: HistoryResponse, gap_from: int, why: str
) -> None:
    """ADR-0008: partial coverage is recorded as partial rather than quietly called complete."""
    service = await backfill_for(dbs, FakeProvider(response))
    outcome = await service.fill(Gap(CHANNEL_ID, CHANNEL_LOGIN, gap_from, 6000))
    await service.writer.stop()
    assert not outcome.complete, why
    async with dbs.chatlog.execute("SELECT complete FROM backfill_runs") as cur:
        assert [r[0] for r in await cur.fetchall()] == [0]


async def test_a_service_error_leaves_the_gap_open(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(error_code="channel_not_joined"))
    service = await backfill_for(dbs, provider)
    outcome = await service.fill(Gap(CHANNEL_ID, CHANNEL_LOGIN, 0, 6000))
    assert (outcome.complete, outcome.error) == (False, "channel_not_joined")
    async with dbs.chatlog.execute("SELECT error FROM backfill_runs") as cur:
        assert [r[0] for r in await cur.fetchall()] == ["channel_not_joined"]


async def test_only_opted_in_channels_are_backfilled_or_kept_warm(dbs: Databases) -> None:
    provider = FakeProvider()
    service = await backfill_for(dbs, provider, opted_in=False)
    assert service.enabled_channels() == []
    assert await service.run_all() == []
    assert await service.keep_warm_once() == 0
    assert provider.calls == []


async def test_a_completed_gap_is_not_fetched_twice(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    service = await backfill_for(dbs, provider)
    await dbs.chatlog.executescript(
        """
        INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason)
             VALUES ('100', 0, 1100, 'shutdown');
        INSERT INTO log_sessions (channel_id, started_at) VALUES ('100', 60000);
        """
    )
    await dbs.chatlog.commit()

    assert len(await service.run_for_channel(CHANNEL_ID, CHANNEL_LOGIN)) == 1
    assert await service.run_for_channel(CHANNEL_ID, CHANNEL_LOGIN) == []  # already filled
    await service.writer.stop()
