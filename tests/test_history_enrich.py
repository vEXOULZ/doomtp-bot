"""Backfill's enrichment: emotes, mentions and cheermotes an IRC line lacks (ADR-0024 §3)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from doomtp_bot.core.events import ChatMessage
from doomtp_bot.history.backfill import BackfillService, Gap, to_events
from doomtp_bot.history.enrich import Enricher
from doomtp_bot.history.irc_parse import parse_line
from doomtp_bot.history.provider import HistoryResponse
from doomtp_bot.storage.db import Connection, Databases
from tests.test_history import CHANNEL_ID, CHANNEL_LOGIN, FakeProvider, backfill_for


@dataclass
class FakeTwitch:
    emotes: list[dict[str, Any]] = field(default_factory=list)
    cheermotes: dict[str, list[int]] = field(default_factory=dict)
    users: dict[str, dict[str, str]] = field(default_factory=dict)
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    async def fetch_emotes(self, channel_id: str) -> list[dict[str, Any]]:
        self.calls.append("emotes")
        if self.fail:
            raise RuntimeError("helix is down")
        return self.emotes

    async def fetch_cheermotes(self, channel_id: str) -> dict[str, list[int]]:
        self.calls.append("cheermotes")
        if self.fail:
            raise RuntimeError("helix is down")
        return self.cheermotes

    async def resolve_user(self, login: str) -> dict[str, str] | None:
        self.calls.append(f"user:{login}")
        if self.fail:
            raise RuntimeError("helix is down")
        return self.users.get(login)


@dataclass
class FakeCdn:
    known: dict[str, list[str]] = field(default_factory=dict)
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    async def formats(self, emote_id: str) -> list[str]:
        self.calls.append(emote_id)
        if self.fail:
            raise RuntimeError("cdn is down")
        return self.known.get(emote_id, [])


def message(text: str, *, tags: str = "", at: int = 1000, nick: str = "alice", user_id: str = "400") -> ChatMessage:
    raw = f"@id=m{at};user-id={user_id};tmi-sent-ts={at}{';' + tags if tags else ''} :{nick}!{nick}@x PRIVMSG #doomtp :{text}"
    line = parse_line(raw)
    assert line is not None
    event = to_events(line, CHANNEL_ID, CHANNEL_LOGIN, raw)
    assert isinstance(event, ChatMessage)
    return event


async def live_emote(conn: Connection, emote: dict[str, Any]) -> None:
    """A live EventSub row that describes `emote`."""
    raw = {"message": {"text": "x", "fragments": [{"type": "emote", "text": "x", "emote": emote}]}}
    await conn.execute(
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, raw, raw_format, sent_at,"
        " received_at) VALUES ('live-1', %s, '1', 'bob', 'x', %s, 'eventsub', 1, 1)",
        (CHANNEL_ID, json.dumps(raw)),
    )


# ── emotes ─────────────────────────────────────────────────────────────────
async def test_an_emote_is_looked_up_in_the_log_then_helix_then_the_cdn(dbs: Databases) -> None:
    await live_emote(dbs.chatlog, {"id": "e-log", "emote_set_id": "s1", "owner_id": "100", "format": ["static"]})
    twitch = FakeTwitch(emotes=[{"id": "e-helix", "set_id": "s2", "owner_id": "100", "formats": ["static"]}])
    cdn = FakeCdn(known={"e-cdn": ["static", "animated"]})
    fill = Enricher(dbs.chatlog, twitch, cdn).fill(CHANNEL_ID)

    found = await fill.message(message("a b c d", tags="emotes=e-log:0-0/e-helix:2-2/e-cdn:4-4/e-gone:6-6"))

    assert found == {
        "emotes": {
            "e-log": {"emote_set_id": "s1", "owner_id": "100", "format": ["static"], "source": "log"},
            "e-helix": {"emote_set_id": "s2", "owner_id": "100", "format": ["static"], "source": "helix"},
            "e-cdn": {
                "emote_set_id": None,
                "owner_id": None,
                "format": ["static", "animated"],
                "source": "cdn",
            },
            "e-gone": {"emote_set_id": None, "owner_id": None, "format": None, "source": "gone"},
        }
    }
    assert sorted(cdn.calls) == ["e-cdn", "e-gone"]


async def test_an_emote_is_looked_up_once_for_good(dbs: Databases) -> None:
    twitch = FakeTwitch(emotes=[{"id": "e1", "set_id": "s", "owner_id": "100", "formats": ["static"]}])
    first = Enricher(dbs.chatlog, twitch, FakeCdn())
    await first.fill(CHANNEL_ID).message(message("a", tags="emotes=e1:0-0"))

    later = FakeTwitch(fail=True)
    found = (
        await Enricher(dbs.chatlog, later, FakeCdn(fail=True))
        .fill(CHANNEL_ID)
        .message(message("a", tags="emotes=e1:0-0"))
    )
    assert found == {"emotes": {"e1": {"emote_set_id": "s", "owner_id": "100", "format": ["static"],
                                       "source": "helix"}}}  # fmt: skip
    assert later.calls == []


async def test_an_emote_nothing_could_answer_for_is_asked_again_later(dbs: Databases) -> None:
    enricher = Enricher(dbs.chatlog, FakeTwitch(fail=True), FakeCdn(fail=True))
    assert await enricher.fill(CHANNEL_ID).message(message("a", tags="emotes=e1:0-0")) is None
    async with await dbs.chatlog.execute("SELECT count(*) AS n FROM emotes") as cur:
        assert (await cur.fetchone() or {})["n"] == 0


# ── mentions ───────────────────────────────────────────────────────────────
async def test_a_reply_names_its_parent(dbs: Databases) -> None:
    tags = "reply-parent-msg-id=p;reply-parent-user-id=300;reply-parent-user-login=mod;reply-parent-display-name=Mod"
    twitch = FakeTwitch()
    found = await Enricher(dbs.chatlog, twitch).fill(CHANNEL_ID).message(message("@Mod hi", tags=tags))
    assert found == {"mentions": {"mod": {"user_id": "300", "user_login": "mod", "user_name": "Mod",
                                          "source": "reply"}}}  # fmt: skip
    assert twitch.calls == []


async def test_someone_who_spoke_earlier_in_the_fill_is_named_from_the_log(dbs: Databases) -> None:
    fill = Enricher(dbs.chatlog, FakeTwitch()).fill(CHANNEL_ID)
    await fill.message(message("hello", nick="bob", user_id="500", tags="display-name=Bob", at=900))
    found = await fill.message(message("hi @bob!"))
    assert found == {"mentions": {"bob": {"user_id": "500", "user_login": "bob", "user_name": "Bob",
                                          "source": "log"}}}  # fmt: skip


async def test_a_name_is_whoever_had_it_at_the_time(dbs: Databases) -> None:
    await dbs.chatlog.execute(
        "INSERT INTO user_names (user_id, login, display_name, seen_from) VALUES"
        " ('1', 'carol', 'Carol', 100), ('2', 'carol', 'CAROL', 5000)"
    )
    fill = Enricher(dbs.chatlog, FakeTwitch()).fill(CHANNEL_ID)
    found = await fill.message(message("@carol hey", at=1000))
    assert found is not None and found["mentions"]["carol"] == {
        "user_id": "1", "user_login": "carol", "user_name": "Carol", "source": "names"
    }  # fmt: skip


async def test_twitch_answers_last_and_only_for_a_login(dbs: Databases) -> None:
    twitch = FakeTwitch(users={"dave": {"id": "700", "name": "dave", "display": "Dave"}})
    fill = Enricher(dbs.chatlog, twitch).fill(CHANNEL_ID)
    found = await fill.message(message("@dave @nobody @ケーキ @dave"))
    assert found == {"mentions": {"dave": {"user_id": "700", "user_login": "dave", "user_name": "Dave",
                                           "source": "twitch"}}}  # fmt: skip
    assert twitch.calls == ["user:dave", "user:nobody"]  # asked once each; `ケーキ` isn't a login


async def test_a_failing_lookup_leaves_the_mention_out(dbs: Databases) -> None:
    fill = Enricher(dbs.chatlog, FakeTwitch(fail=True)).fill(CHANNEL_ID)
    assert await fill.message(message("@dave hi")) is None


# ── cheermotes ─────────────────────────────────────────────────────────────
async def test_a_cheermote_gets_its_tier(dbs: Databases) -> None:
    twitch = FakeTwitch(cheermotes={"cheer": [1, 100, 1000, 5000, 10000, 100000], "doodle": [1, 100]})
    fill = Enricher(dbs.chatlog, twitch).fill(CHANNEL_ID)
    found = await fill.message(message("Cheer150 doodle5 cheerful nope10", tags="bits=155"))
    assert found == {
        "cheermotes": {
            "cheer150": {"prefix": "cheer", "bits": 150, "tier": 100, "source": "twitch"},
            "doodle5": {"prefix": "doodle", "bits": 5, "tier": 1, "source": "twitch"},
        }
    }


async def test_a_message_without_bits_has_no_cheermotes(dbs: Databases) -> None:
    twitch = FakeTwitch(cheermotes={"cheer": [1]})
    assert await Enricher(dbs.chatlog, twitch).fill(CHANNEL_ID).message(message("Cheer100")) is None
    assert twitch.calls == []


# ── in a fill ──────────────────────────────────────────────────────────────
async def test_a_fill_stores_what_it_found(dbs: Databases) -> None:
    raw = (
        "@id=m1;user-id=400;tmi-sent-ts=1000;emotes=e1:0-1;reply-parent-msg-id=p;reply-parent-user-id=300;"
        "reply-parent-user-login=mod :alice!alice@x PRIVMSG #doomtp :hi @mod"
    )
    service = await backfill_for(dbs, FakeProvider(HistoryResponse(lines=(raw,))))
    twitch = FakeTwitch(emotes=[{"id": "e1", "set_id": "s", "owner_id": "100", "formats": ["static"]}])
    service = BackfillService(conn=service.conn, writer=service.writer, provider=service.provider,
                              policy=service.policy, enricher=Enricher(dbs.chatlog, twitch))  # fmt: skip
    await service.fill(Gap(CHANNEL_ID, CHANNEL_LOGIN, 900, 2000))
    await service.writer.stop()

    async with await dbs.chatlog.execute("SELECT enrichment FROM messages WHERE message_id = 'm1'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert set(row["enrichment"]) == {"emotes", "mentions"}
    assert row["enrichment"]["mentions"]["mod"]["source"] == "reply"


async def test_a_live_message_keeps_its_enrichment_empty(dbs: Databases) -> None:
    service = await backfill_for(dbs, FakeProvider())
    await service.writer.message(replace(message("hi"), raw_line=None, source="eventsub"))
    await service.writer.stop()
    async with await dbs.chatlog.execute("SELECT enrichment FROM messages") as cur:
        assert [r["enrichment"] for r in await cur.fetchall()] == [None]
