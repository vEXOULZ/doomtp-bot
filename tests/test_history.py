"""History backfill: IRC parsing, gap detection and filling (ADR-0008)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from doomtp_bot.chatlog import events
from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.core import metrics
from doomtp_bot.core.events import ChatCleared, ChatMessage, ChatNotification, MessageDeleted
from doomtp_bot.core.events import UserMessagesCleared as UserCleared
from doomtp_bot.history.backfill import BackfillService, Gap, find_gaps, to_events
from doomtp_bot.history.irc_parse import badges, parse_line, unescape_tag
from doomtp_bot.history.provider import PAUSED, HistoryResponse
from doomtp_bot.policy.repository import Actor
from doomtp_bot.storage.db import Databases
from tests.fakes import policy_with_channels

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"

PRIVMSG = (
    "@badge-info=subscriber/12;badges=subscriber/12,moderator/1;color=#1E90FF;display-name=Alice;"
    "id=abc-123;user-id=400;tmi-sent-ts=1000 "
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


def privmsg(at: int, message_id: str) -> str:
    """A chat line sent at `at`."""
    return f"@id={message_id};user-id=400;tmi-sent-ts={at} :alice!alice@x PRIVMSG #doomtp :line {message_id}"


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
    assert event.source == "ivr-logs" and event.raw_line == PRIVMSG
    assert event.sent_at == 1000 and event.received_at == 1000


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
    await dbs.chatlog.execute(
        """
        INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason)
             VALUES ('100', 1000, 2000, 'shutdown'), ('100', 9000, 9500, 'shutdown');
        INSERT INTO log_sessions (channel_id, started_at) VALUES ('100', 20000);
        INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason)
             VALUES ('200', 1000, 2000, 'shutdown');
        """
    )
    gaps = await find_gaps(dbs.chatlog, CHANNEL_ID, CHANNEL_LOGIN)
    assert [(g.from_ms, g.to_ms) for g in gaps] == [(2000, 9000), (9500, 20000)]


async def test_short_interruptions_are_not_gaps(dbs: Databases) -> None:
    await dbs.chatlog.execute(
        """
        INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason)
             VALUES ('100', 1000, 2000, 'shutdown');
        INSERT INTO log_sessions (channel_id, started_at) VALUES ('100', 2100);
        """
    )
    assert await find_gaps(dbs.chatlog, CHANNEL_ID, CHANNEL_LOGIN) == []


# ── filling them ───────────────────────────────────────────────────────────
@dataclass
class FakeProvider:
    """Gives `response` to every request, or each of `responses` in turn and then `response`."""

    response: HistoryResponse = field(default_factory=HistoryResponse)
    responses: list[HistoryResponse] = field(default_factory=list)
    calls: list[tuple[str, int, int, int, int]] = field(default_factory=list)

    async def fetch(
        self, channel_id: str, *, from_ms: int, to_ms: int, limit: int = 1000, offset: int = 0
    ) -> HistoryResponse:
        self.calls.append((channel_id, from_ms, to_ms, limit, offset))
        return self.responses.pop(0) if self.responses else self.response


async def backfill_for(dbs: Databases, provider: FakeProvider, *, opted_in: bool = True) -> BackfillService:
    policy = await policy_with_channels(dbs.bot, (CHANNEL_ID, CHANNEL_LOGIN))
    await policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "history_backfill", opted_in, Actor(None, "s"))
    )
    writer = ChatLogWriter(dbs.chatlog)
    return BackfillService(conn=dbs.chatlog, writer=writer, provider=provider, policy=policy)


async def test_a_gap_is_filled_from_history_and_recorded(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG, CLEARMSG, USERNOTICE)))
    service = await backfill_for(dbs, provider)
    gap = Gap(CHANNEL_ID, CHANNEL_LOGIN, 1100, 6000)

    inserted = metrics.BACKFILL_INSERTED.value()
    logged = metrics.MESSAGES_LOGGED.value(source="ivr-logs")
    outcome = await service.fill(gap)
    await service.writer.stop()

    assert metrics.BACKFILL_INSERTED.value() - inserted == 3
    assert metrics.MESSAGES_LOGGED.value(source="ivr-logs") - logged == 1  # one PRIVMSG among them
    # From 5s before the gap (clamped at 0) to its end; the end the service takes is exclusive.
    assert provider.calls == [(CHANNEL_ID, 0, 6001, 1000, 0)]
    assert (outcome.fetched, outcome.inserted, outcome.complete) == (3, 3, True)
    async with await dbs.chatlog.execute(
        "SELECT message_id, source, raw_format, raw->>'line' AS line FROM messages"
    ) as cur:
        rows = [tuple(r.values()) for r in await cur.fetchall()]
    assert rows == [("abc-123", "ivr-logs", "irc", PRIVMSG)]
    async with await dbs.chatlog.execute(
        "SELECT type, raw_format, raw ? 'line' AS kept FROM mod_events"
    ) as cur:
        assert [tuple(r.values()) for r in await cur.fetchall()] == [("delete", "irc", True)]
    async with await dbs.chatlog.execute(
        "SELECT deleted_at FROM messages WHERE message_id = 'abc-123'"
    ) as cur:
        assert (await cur.fetchone())[
            "deleted_at"
        ] == 2000  # the CLEARMSG flagged it, without deleting the row
    async with await dbs.chatlog.execute(
        "SELECT fetched, inserted, complete, provider FROM backfill_runs"
    ) as cur:
        assert [tuple(r.values()) for r in await cur.fetchall()] == [(3, 3, 1, "ivr-logs")]


@dataclass
class ServiceLike:
    """Answers the way logs.ivr.fi does: lines from `from_ms` (inclusive) to `to_ms` (exclusive), oldest
    first, `limit` of them after skipping `offset`."""

    lines: list[tuple[int, str]]
    calls: list[tuple[int, int, int]] = field(default_factory=list)

    async def fetch(
        self, channel_id: str, *, from_ms: int, to_ms: int, limit: int = 1000, offset: int = 0
    ) -> HistoryResponse:
        self.calls.append((from_ms, to_ms, offset))
        found = [line for at, line in sorted(self.lines) if from_ms <= at < to_ms][offset : offset + limit]
        return HistoryResponse(tuple(found), hit_limit=len(found) >= limit)


def chat(*at: int) -> list[tuple[int, str]]:
    return [(t, privmsg(t, f"m{t}")) for t in at]


async def service_with(dbs: Databases, provider: ServiceLike) -> BackfillService:
    service = await backfill_for(dbs, FakeProvider())
    service.provider = provider
    return service


async def stored(dbs: Databases) -> list[str]:
    async with await dbs.chatlog.execute("SELECT message_id FROM messages ORDER BY sent_at") as cur:
        return [r["message_id"] for r in await cur.fetchall()]


async def test_a_range_from_months_ago_is_filled(dbs: Databases) -> None:
    """Why logs.ivr.fi: a range far older than a day, with plenty of newer chat after it."""
    provider = ServiceLike(chat(10000, 12000, 14000, *range(100000, 101000)))
    service = await service_with(dbs, provider)
    outcome = await service.fill(Gap(CHANNEL_ID, CHANNEL_LOGIN, 10000, 20000))
    await service.writer.stop()

    assert provider.calls == [(5000, 20001, 0)]
    assert (outcome.inserted, outcome.complete) == (3, True)
    assert await stored(dbs) == ["m10000", "m12000", "m14000"]


async def test_each_gap_is_asked_for_on_its_own(dbs: Databases) -> None:
    provider = ServiceLike(chat(1005, 3000, 50000, 80000, 95000))
    service = await service_with(dbs, provider)
    gaps = [Gap(CHANNEL_ID, CHANNEL_LOGIN, 70000, 90000), Gap(CHANNEL_ID, CHANNEL_LOGIN, 1100, 6000)]
    result = await service.fill_many(gaps)
    await service.writer.stop()

    assert provider.calls == [(0, 6001, 0), (65000, 90001, 0)]  # oldest first, each from 5s early
    assert (result.fetched, result.inserted, result.complete) == (3, 3, True)
    assert [(o.gap.from_ms, o.inserted, o.complete) for o in result.outcomes] == [
        (1100, 2, True),
        (70000, 1, True),
    ]
    assert await stored(dbs) == ["m1005", "m3000", "m80000"]  # not the chat between them


async def test_a_full_page_is_followed_by_the_next(dbs: Databases, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("doomtp_bot.history.backfill.DEFAULT_LIMIT", 2)
    provider = ServiceLike(chat(1000, 2000, 3000, 4000, 9000))
    service = await service_with(dbs, provider)
    outcome = await service.fill(Gap(CHANNEL_ID, CHANNEL_LOGIN, 1100, 5000))
    await service.writer.stop()

    assert provider.calls == [(0, 5001, 0), (0, 5001, 2), (0, 5001, 4)]
    assert (outcome.inserted, outcome.complete, outcome.reached_ms) == (4, True, 4000)


async def test_a_service_error_leaves_the_gaps_open(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(error_code="channel_not_logged"))
    service = await backfill_for(dbs, provider)
    gaps = [Gap(CHANNEL_ID, CHANNEL_LOGIN, 0, 6000), Gap(CHANNEL_ID, CHANNEL_LOGIN, 9000, 10000)]
    incomplete = metrics.BACKFILL_INCOMPLETE.value()
    result = await service.fill_many(gaps)
    assert len(provider.calls) == 1  # the second gap isn't asked for: the answer would be the same
    assert [(o.complete, o.error) for o in result.outcomes] == [(False, "channel_not_logged")] * 2
    assert metrics.BACKFILL_INCOMPLETE.value() - incomplete == 2
    async with await dbs.chatlog.execute("SELECT error FROM backfill_runs") as cur:
        assert [r["error"] for r in await cur.fetchall()] == ["channel_not_logged"] * 2


async def test_a_paused_fill_resumes_where_it_stopped(dbs: Databases) -> None:
    paused = HistoryResponse(error_code=PAUSED, retry_at_ms=86_400_000)
    first_page = HistoryResponse((privmsg(2000, "a"), privmsg(3000, "b")), hit_limit=True)
    provider = FakeProvider(responses=[first_page, paused])
    service = await backfill_for(dbs, provider)
    gap = Gap(CHANNEL_ID, CHANNEL_LOGIN, 1100, 6000)

    result = await service.fill_many([gap])
    assert result.retry_at_ms == 86_400_000
    (outcome,) = result.outcomes
    assert (outcome.inserted, outcome.complete, outcome.error, outcome.reached_ms) == (2, False, PAUSED, 3000)

    provider.response = HistoryResponse((privmsg(3000, "b"), privmsg(4000, "c")))
    (again,) = (await service.fill_many([gap])).outcomes
    await service.writer.stop()
    assert provider.calls[-1] == (CHANNEL_ID, 3000, 6001, 1000, 0)  # from the newest line stored
    assert again.complete
    assert await stored(dbs) == ["a", "b", "c"]


async def test_only_opted_in_channels_are_backfilled(dbs: Databases) -> None:
    provider = FakeProvider()
    service = await backfill_for(dbs, provider, opted_in=False)
    assert service.enabled_channels() == []
    assert service.login_if_enabled(CHANNEL_ID) is None
    assert provider.calls == []


async def test_a_completed_gap_is_not_fetched_twice(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    service = await backfill_for(dbs, provider)
    await dbs.chatlog.execute(
        """
        INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason)
             VALUES ('100', 0, 1100, 'shutdown');
        INSERT INTO log_sessions (channel_id, started_at) VALUES ('100', 60000);
        """
    )

    (gap,) = await service.open_gaps(CHANNEL_ID, CHANNEL_LOGIN)
    await service.fill(gap)
    assert await service.open_gaps(CHANNEL_ID, CHANNEL_LOGIN) == []  # already filled
    await service.writer.stop()


async def test_a_gap_that_took_two_runs_to_fill_is_not_fetched_again(dbs: Databases) -> None:
    """Any complete run settles a gap, whatever order the earlier incomplete ones were recorded in."""
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    service = await backfill_for(dbs, provider)
    await dbs.chatlog.execute(
        """
        INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason)
             VALUES ('100', 0, 1100, 'shutdown');
        INSERT INTO log_sessions (channel_id, started_at) VALUES ('100', 60000);
        INSERT INTO backfill_runs (channel_id, gap_from, gap_to, fetched, inserted, complete, error, at)
             VALUES ('100', 1100, 60000, 0, 0, false, 'channel_not_logged', 1),
                    ('100', 1100, 60000, 1, 1, true, '', 2);
        """
    )

    assert await service.open_gaps(CHANNEL_ID, CHANNEL_LOGIN) == []
    await service.writer.stop()


async def test_a_gap_recent_messages_could_not_reach_is_open_again(dbs: Databases) -> None:
    """recent-messages kept about a day and recorded older gaps as out of its reach; ivr.fi can fill them."""
    service = await backfill_for(dbs, FakeProvider())
    await dbs.chatlog.execute(
        """
        INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason)
             VALUES ('100', 0, 1100, 'shutdown');
        INSERT INTO log_sessions (channel_id, started_at) VALUES ('100', 60000);
        INSERT INTO backfill_runs (channel_id, gap_from, gap_to, complete, error, provider, at)
             VALUES ('100', 1100, 60000, false, 'out_of_reach', 'recent-messages', 1);
        """
    )
    gaps = await service.open_gaps(CHANNEL_ID, CHANNEL_LOGIN)
    assert [(g.from_ms, g.to_ms) for g in gaps] == [(1100, 60000)]


async def test_a_delete_names_the_author_by_id(dbs: Databases) -> None:
    """Twitch's CLEARMSG gives only the author's login: the id comes from the message it deleted."""
    in_fill = "@login=alice;target-msg-id=m-new;tmi-sent-ts=3000 :tmi.twitch.tv CLEARMSG #doomtp :x"
    logged = "@login=bob;target-msg-id=m-old;tmi-sent-ts=3100 :tmi.twitch.tv CLEARMSG #doomtp :x"
    unknown = "@login=carol;target-msg-id=m-gone;tmi-sent-ts=3200 :tmi.twitch.tv CLEARMSG #doomtp :x"
    await dbs.chatlog.execute(
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, raw, raw_format, sent_at,"
        " received_at) VALUES ('m-old', '100', '500', 'bob', 'old', '{}', 'legacy', 500, 500)"
    )
    provider = FakeProvider(HistoryResponse((privmsg(2000, "m-new"), in_fill, logged, unknown)))
    service = await backfill_for(dbs, provider)
    await service.fill(Gap(CHANNEL_ID, CHANNEL_LOGIN, 1100, 6000))
    await service.writer.stop()
    async with await dbs.chatlog.execute(
        "SELECT message_id, target_user_id FROM mod_events ORDER BY at"
    ) as cur:
        assert [tuple(r.values()) for r in await cur.fetchall()] == [
            ("m-new", "400"),
            ("m-old", "500"),
            ("m-gone", None),  # not in the log: unknown, rather than a login in a user id column
        ]


async def test_a_backfilled_timeout_keeps_its_length(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse((CLEARCHAT_USER,)))
    service = await backfill_for(dbs, provider)
    await service.fill(Gap(CHANNEL_ID, CHANNEL_LOGIN, 1100, 6000))
    await service.writer.stop()
    async with await dbs.chatlog.execute(
        "SELECT type, target_user_id, raw, raw_format FROM mod_events"
    ) as cur:
        found = await cur.fetchall()
    assert [(r["type"], r["target_user_id"]) for r in found] == [("user_clear", "400")]
    assert events.moderation(found[0]["raw_format"], found[0]["raw"])["irc"]["ban-duration"] == "600"
