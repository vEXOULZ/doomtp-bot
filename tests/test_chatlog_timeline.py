"""`/api/v1/channels/{login}/log` and `/log/coverage`: the chat log as one paged timeline (ADR-0025)."""
# ruff: noqa: F811  (the imported fixtures are parameters here, which ruff reads as redefinitions)

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.chatlog.timeline import Cursor, CursorError
from doomtp_bot.policy.repository import Actor
from doomtp_bot.storage.db import Connection
from tests.test_api_data import (  # noqa: F401  (fixtures)
    CHANNEL_ID,
    CHANNEL_LOGIN,
    app_and_keys,
    auth,
    client,
    write_key,
)

LOG = f"/api/v1/channels/{CHANNEL_LOGIN}/log"
T = 1_700_000_000_000


async def _message(
    chatlog: Connection,
    message_id: str,
    at: int,
    *,
    user: tuple[str, str] = ("400", "alice"),
    text: str = "hi",
    is_self: bool = False,
    is_command: bool = False,
    deleted_at: int | None = None,
) -> None:
    raw = {"message": {"text": text, "fragments": [{"type": "text", "text": text}]},
           "badges": [{"set_id": "vip", "id": "1"}]}  # fmt: skip
    await chatlog.execute(
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, is_self, is_command, raw,"
        " raw_format, sent_at, received_at, deleted_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'legacy', %s, %s, %s)",
        (message_id, CHANNEL_ID, *user, text, is_self, is_command, json.dumps(raw), at, at + 5, deleted_at),
    )  # fmt: skip


async def _users(chatlog: Connection) -> None:
    for user_id, login, since in (
        ("400", "alice", T - 1000),
        ("400", "alice_old", T - 9000),
        ("300", "mod", T),
    ):
        await chatlog.execute(
            "INSERT INTO user_names (user_id, login, seen_from) VALUES (%s, %s, %s)", (user_id, login, since)
        )
    for user_id, login in (("400", "alice"), ("300", "mod")):
        await chatlog.execute(
            "INSERT INTO users (user_id, login, display_name, first_seen, last_seen) VALUES (%s, %s, %s, %s, %s)",
            (user_id, login, login.title(), T, T),
        )


@pytest.fixture
async def chatlog(app_and_keys: tuple[Any, ApiKeyService]) -> Connection:
    """A small log: messages, a sub, a timeout, all around T, with ties on the same millisecond."""
    conn: Connection = app_and_keys[0].state.chatlog
    await _users(conn)
    await _message(conn, "m-a", T)
    await _message(conn, "m-b", T)  # same instant as m-a: the id breaks the tie
    await _message(conn, "m-c", T + 10, text="the deleted one", deleted_at=T + 20)
    await _message(conn, "m-d", T + 30, user=("200", "friend"), text="cafe au lait")
    await conn.execute(
        "INSERT INTO chat_notifications (id, channel_id, user_id, type, raw, raw_format, sent_at)"
        " VALUES ('n-1', %s, '400', 'sub', %s, 'legacy', %s)",
        # same instant as the messages: kind breaks the tie
        (CHANNEL_ID, json.dumps({"notice_type": "sub", "legacy": {"tier": "1000"}}), T),
    )
    await conn.execute(
        "INSERT INTO mod_events (channel_id, type, message_id, target_user_id, moderator_user_id, raw,"
        " raw_format, at) VALUES (%s, 'timeout', NULL, '400', '300', %s, 'legacy', %s)",
        (CHANNEL_ID, json.dumps({"duration_s": 60, "reason": "spam"}), T + 10),
    )
    return conn


async def _all(client: httpx.AsyncClient, key: str, **params: Any) -> list[dict[str, Any]]:
    """Every entry, a page of `limit` at a time, following `next`."""
    found: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = await client.get(
            LOG, params={**params, **({"cursor": cursor} if cursor else {})}, headers=auth(key)
        )
        assert page.status_code == 200, page.text
        body = page.json()
        found += body["entries"]
        cursor = body["next"]
        if cursor is None:
            return found


def _ids(entries: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return [(e["kind"], e["id"]) for e in entries]


async def test_the_log_is_one_timeline_newest_first(
    client: httpx.AsyncClient, chatlog: Connection, write_key: str
) -> None:
    body = (await client.get(LOG, headers=auth(write_key))).json()
    mod_id = body["entries"][1]["id"]
    assert _ids(body["entries"]) == [
        ("message", "m-d"),
        ("moderation", mod_id),
        ("message", "m-c"),
        ("notification", "n-1"),
        ("message", "m-b"),
        ("message", "m-a"),
    ]
    assert body["next"] is None and body["order"] == "desc"

    timeout = body["entries"][1]
    assert timeout["type"] == "timeout" and timeout["duration_s"] == 60 and timeout["reason"] == "spam"
    assert timeout["target"]["login"] == "alice" and timeout["moderator"]["login"] == "mod"
    sub = body["entries"][3]
    assert sub["payload"] == {"tier": "1000"} and sub["user"]["display_name"] == "Alice"
    deleted = body["entries"][2]
    assert deleted["deleted_at"] == T + 20 and deleted["text"] == "the deleted one"
    assert deleted["badges"] == [{"set_id": "vip", "id": "1"}] and deleted["fragments"][0]["type"] == "text"


async def test_backfilled_rows_are_read_from_their_irc_lines(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    conn: Connection = app_and_keys[0].state.chatlog
    await _users(conn)
    message = (
        "@id=m-irc;user-id=400;display-name=Alice;color=#FF0000;badges=vip/1;emotes=25:8-12;"
        "reply-parent-msg-id=m-0;reply-parent-user-id=300;reply-parent-user-login=mod;"
        f"reply-parent-display-name=Mod;tmi-sent-ts={T} :alice!alice@x PRIVMSG #{CHANNEL_LOGIN} :@Mod hi Kappa"
    )
    enrichment = {
        "emotes": {"25": {"emote_set_id": "0", "owner_id": "0", "format": ["static"], "source": "helix"}},
        "mentions": {"mod": {"user_id": "300", "user_login": "mod", "user_name": "Mod", "source": "reply"}},
    }
    await conn.execute(
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, is_self, is_command, raw,"
        " raw_format, enrichment, source, sent_at, received_at)"
        " VALUES ('m-irc', %s, '400', 'alice', '@Mod hi Kappa', false, false, %s, 'irc', %s, 'ivr-logs', %s, %s)",
        (CHANNEL_ID, json.dumps({"line": message}), json.dumps(enrichment), T, T),
    )
    raid = (
        f"@id=n-irc;user-id=400;login=alice;display-name=Alice;msg-id=raid;msg-param-viewerCount=8;"
        f"msg-param-login=alice;msg-param-displayName=Alice;system-msg=8\\sraiders;tmi-sent-ts={T + 1}"
        f" :tmi.twitch.tv USERNOTICE #{CHANNEL_LOGIN}"
    )
    await conn.execute(
        "INSERT INTO chat_notifications (id, channel_id, user_id, type, raw, raw_format, source, sent_at)"
        " VALUES ('n-irc', %s, '400', 'raid', %s, 'irc', 'ivr-logs', %s)",
        (CHANNEL_ID, json.dumps({"line": raid}), T + 1),
    )
    clear = f"@ban-duration=600;target-user-id=400;tmi-sent-ts={T + 2} :tmi.twitch.tv CLEARCHAT #{CHANNEL_LOGIN} :alice"
    await conn.execute(
        "INSERT INTO mod_events (channel_id, type, target_user_id, raw, raw_format, source, at)"
        " VALUES (%s, 'timeout', '400', %s, 'irc', 'ivr-logs', %s)",
        (CHANNEL_ID, json.dumps({"line": clear}), T + 2),
    )

    timeout, notice, entry = (await client.get(LOG, headers=auth(write_key))).json()["entries"]
    assert timeout["duration_s"] == 600 and timeout["reason"] is None
    assert notice["payload"] == {
        "system_message": "8 raiders",
        "text": "",
        "chatter": {"id": "400", "login": "alice"},
        "detail": {"user_id": "400", "user_login": "alice", "user_name": "Alice", "viewer_count": 8,
                   "profile_image_url": None},
    }  # fmt: skip
    assert entry["user"] == {"id": "400", "login": "alice", "display_name": "Alice"}
    assert entry["color"] == "#FF0000"
    assert entry["badges"] == [{"set_id": "vip", "id": "1", "info": ""}]
    assert entry["reply_parent_id"] == "m-0"
    assert entry["reply_parent_user"] == {"id": "300", "login": "mod", "display_name": "Mod"}
    assert entry["fragments"] == [
        {"type": "mention", "text": "@Mod",
         "mention": {"id": "300", "login": "mod", "user_name": "Mod", "source": "reply"}},
        {"type": "text", "text": " hi "},
        {"type": "emote", "text": "Kappa", "emote_id": "25",
         "emote": {"emote_set_id": "0", "owner_id": "0", "format": ["static"], "source": "helix"}},
    ]  # fmt: skip


@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("limit", [1, 2, 4])
async def test_pages_follow_on_without_gaps_or_repeats(
    client: httpx.AsyncClient, chatlog: Connection, write_key: str, order: str, limit: int
) -> None:
    whole = (await client.get(LOG, params={"order": order}, headers=auth(write_key))).json()["entries"]
    paged = await _all(client, write_key, order=order, limit=limit)
    assert _ids(paged) == _ids(whole)
    assert len(whole) == 6


async def test_filters_narrow_the_timeline(
    client: httpx.AsyncClient, chatlog: Connection, write_key: str
) -> None:
    async def ids(**params: Any) -> list[tuple[str, Any]]:
        return _ids(await _all(client, write_key, order="asc", **params))

    # A window is inclusive at since and exclusive at until.
    window = await ids(since=T + 10, until=T + 30)
    assert [kind for kind, _ in window] == ["message", "moderation"] and window[0] == ("message", "m-c")
    assert await ids(kind=["notification"]) == [("notification", "n-1")]
    assert [k for k, _ in await ids(kind=["message", "moderation"])].count("notification") == 0
    # By name, including a login the user had before a rename; moderation matches on its target.
    by_old_name = await ids(user="alice_old")
    assert ("message", "m-d") not in by_old_name and ("message", "m-a") in by_old_name
    assert "moderation" in [k for k, _ in by_old_name]
    assert await ids(user="nobody") == []
    assert ("message", "m-c") not in await ids(hide_removed=True)
    # Search reads messages only, unaccented as the chat search is.
    assert await ids(q="café") == [("message", "m-d")]


async def test_a_command_and_the_reply_it_caused_point_at_the_same_run(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    conn: Connection = app_and_keys[0].state.chatlog
    await _message(conn, "cmd", T, text="!ping", is_command=True)
    await _message(conn, "reply", T + 400, user=("999", "doomtp_bot"), text="pong", is_self=True)
    await _message(conn, "plain", T + 500)
    await conn.execute(
        "INSERT INTO command_runs (channel_id, user_id, trigger_type, trigger_id, expr, code, message,"
        " duration_ms, run_ref, at) VALUES (%s, '400', 'chat', 'cmd', '!ping', 0, 'pong', 3, 'r1', %s)",
        (CHANNEL_ID, T + 300),
    )
    await conn.execute(
        "INSERT INTO outbound_msgs (channel_id, text_sent, twitch_message_id, run_ref, at)"
        " VALUES (%s, 'pong', 'reply', 'r1', %s)",
        (CHANNEL_ID, T + 390),
    )
    entries = (await client.get(LOG, params={"order": "asc"}, headers=auth(write_key))).json()["entries"]
    runs = {e["id"]: e["run"] for e in entries}
    assert runs["cmd"] == {
        "ref": "r1", "trigger_type": "chat", "trigger_id": "cmd", "expr": "!ping", "code": 0, "message": "pong"
    }  # fmt: skip
    assert runs["reply"] == runs["cmd"]  # the replay can draw the reply back to "cmd"
    assert runs["plain"] is None


async def test_a_cursor_is_checked(client: httpx.AsyncClient, chatlog: Connection, write_key: str) -> None:
    first = (await client.get(LOG, params={"limit": 1}, headers=auth(write_key))).json()
    # A cursor pages the order it was read in; the other order would start from the wrong end.
    turned = await client.get(LOG, params={"cursor": first["next"], "order": "asc"}, headers=auth(write_key))
    assert turned.status_code == 400
    for junk in ("nonsense", Cursor("asc", T, 2, "not-a-number").encode(), Cursor("asc", T, 9, "x").encode()):
        assert (await client.get(LOG, params={"cursor": junk}, headers=auth(write_key))).status_code == 400
    with pytest.raises(CursorError):
        Cursor.decode("W10")  # "[]"


async def test_a_public_log_shows_what_chat_saw_and_can_be_closed(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], chatlog: Connection
) -> None:
    """Without signing in: messages and notifications only, no removed messages (ADR-0026)."""
    body = (await client.get(LOG)).json()
    assert _ids(body["entries"]) == [
        ("message", "m-d"),
        ("notification", "n-1"),
        ("message", "m-b"),
        ("message", "m-a"),
    ]
    assert (await client.get(f"{LOG}/coverage", params={"since": T})).status_code == 200

    await app_and_keys[0].state.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "public_log", False, Actor(None, "test"))
    )
    assert (await client.get(LOG)).status_code == 401
    assert (await client.get(f"{LOG}/coverage", params={"since": T})).status_code == 401


# ── coverage ────────────────────────────────────────────────────────────────
async def _session(conn: Connection, start: int, end: int | None, reason: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason) VALUES (%s, %s, %s, %s)",
        (CHANNEL_ID, start, end, reason),
    )


async def test_coverage_names_every_hole_and_what_filled_it(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    conn: Connection = app_and_keys[0].state.chatlog
    await _session(conn, T, T + 60_000, "update")
    await _session(conn, T + 90_000, T + 120_000, "crash")
    await _session(conn, T + 125_000, T + 200_000, "part")
    await conn.execute(
        "INSERT INTO backfill_runs (channel_id, gap_from, gap_to, fetched, inserted, complete, at)"
        " VALUES (%s, %s, %s, 10, 10, true, %s)",
        (CHANNEL_ID, T + 60_000, T + 90_000, T + 91_000),
    )

    def get(since: int, until: int) -> Any:
        return client.get(f"{LOG}/coverage", params={"since": since, "until": until}, headers=auth(write_key))

    whole = (await get(T - 5_000, T + 210_000)).json()
    assert [(g["from"], g["to"], g["reason"]) for g in whole["gaps"]] == [
        (T - 5_000, T, "before_log"),
        (T + 60_000, T + 90_000, "between_sessions"),
        (T + 120_000, T + 125_000, "between_sessions"),
        (T + 200_000, T + 210_000, "not_listening"),
    ]
    assert whole["gaps"][1]["backfill"] == {
        "complete": True,
        "inserted": 10,
        "error": None,
        "provider": "ivr-logs",
    }
    assert whole["gaps"][2]["backfill"] is None and whole["complete"] is False
    assert len(whole["sessions"]) == 3

    # A window inside one session, or across a gap backfill filled, is complete.
    inside = (await get(T + 1_000, T + 2_000)).json()
    assert inside["gaps"] == [] and inside["complete"] is True and len(inside["sessions"]) == 1
    filled = (await get(T + 50_000, T + 100_000)).json()
    assert [g["reason"] for g in filled["gaps"]] == ["between_sessions"] and filled["complete"] is True
    # Holes are clipped to the window.
    assert filled["gaps"][0]["from"] == T + 60_000 and filled["gaps"][0]["to"] == T + 90_000
    assert (await get(T, T)).status_code == 422


async def test_coverage_of_a_channel_never_logged_is_one_hole(
    client: httpx.AsyncClient, chatlog: Connection, write_key: str
) -> None:
    body = (
        await client.get(f"{LOG}/coverage", params={"since": T, "until": T + 1}, headers=auth(write_key))
    ).json()
    assert body["gaps"] == [{"from": T, "to": T + 1, "reason": "before_log", "backfill": None}]
    assert body["sessions"] == [] and body["complete"] is False
