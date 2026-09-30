"""Events kept as Twitch sent them (ADR-0024 items 1 and 2): chatlog migration 0002 and the writer."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from typing import Any

import psycopg
from alembic import command

from doomtp_bot.chatlog import legacy
from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.core.events import Badge, ChatCleared, ChatMessage, ChatNotification, MessageDeleted
from doomtp_bot.storage import schema
from doomtp_bot.storage.db import Databases

IRC_LINE = "@id=m2;user-id=u2 :bob!bob@bob.tmi.twitch.tv PRIVMSG #doomtp :hello"

FULL = ChatMessage(
    message_id="m1", channel_id="c1", channel_login="doomtp", user_id="u1", user_login="alice",
    display_name="Alice", text="hi Kappa @bob cheer100", sent_at=1000, received_at=1005,
    badges=(Badge("subscriber", "3", "14"), Badge("moderator", "1")),
    fragments=(
        {"type": "text", "text": "hi "},
        {"type": "emote", "text": "Kappa", "emote_id": "25"},
        {"type": "mention", "text": "@bob", "mention": {"id": "u2", "login": "bob"}},
        {"type": "cheermote", "text": "cheer100", "cheermote": {"prefix": "cheer", "bits": 100}},
    ),
    bits=100, reply_parent_id="p1", reward_id="r1", source_channel_id="s1",
)  # fmt: skip
PLAIN = ChatMessage(
    message_id="m3", channel_id="c1", channel_login="doomtp", user_id="u3", user_login="carol",
    display_name="carol", text="plain", sent_at=3000, received_at=3000,
)  # fmt: skip
NOTICE = ChatNotification(
    id="n1", channel_id="c1", user_id=None, type="announcement",
    payload={"system_message": "hear ye", "chatter": None, "detail": {"color": "BLUE"}}, sent_at=4000,
)  # fmt: skip


def _insert_before_0002(conn: psycopg.Connection[Any], msg: ChatMessage, raw: str | None = None) -> None:
    """A row as the writer stored it before ADR-0024: its own JSON in `badges` and `fragments`."""
    conn.execute(
        "INSERT INTO chatlog.messages (message_id, channel_id, user_id, user_login, display_name, text,"
        " message_type, badges, fragments, bits, reply_parent_id, reward_id, source_channel_id, source, raw,"
        " sent_at, received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            msg.message_id, msg.channel_id, msg.user_id, msg.user_login, msg.display_name, msg.text,
            msg.message_type, json.dumps([asdict(b) for b in msg.badges]), json.dumps(list(msg.fragments)),
            msg.bits, msg.reply_parent_id, msg.reward_id, msg.source_channel_id,
            "eventsub" if raw is None else "recent-messages", raw, msg.sent_at, msg.received_at,
        ),
    )  # fmt: skip


def test_0002_keeps_irc_lines_and_rebuilds_the_rest_as_the_writer_would(empty_database: str) -> None:
    config = schema.config("chatlog", empty_database)
    command.upgrade(config, "0001")
    with psycopg.connect(empty_database) as conn:
        _insert_before_0002(conn, FULL)
        _insert_before_0002(conn, PLAIN)
        _insert_before_0002(conn, replace(PLAIN, message_id="m2"), IRC_LINE)
        conn.execute(
            "INSERT INTO chatlog.chat_notifications (id, channel_id, user_id, type, payload, sent_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                NOTICE.id,
                NOTICE.channel_id,
                NOTICE.user_id,
                NOTICE.type,
                json.dumps(NOTICE.payload),
                NOTICE.sent_at,
            ),
        )
        conn.execute(
            "INSERT INTO chatlog.mod_events (channel_id, type, message_id, target_user_id, at)"
            " VALUES ('c1', 'delete', 'm1', 'u1', 5000)"
        )
        conn.execute(
            "INSERT INTO chatlog.backfill_runs (channel_id, gap_from, gap_to, at) VALUES ('c1', 1, 2, 3)"
        )

    command.upgrade(config, "0002")
    with psycopg.connect(empty_database) as conn:
        messages = dict(
            (r[0], (r[1], r[2]))
            for r in conn.execute("SELECT message_id, raw_format, raw FROM chatlog.messages").fetchall()
        )
        notice = conn.execute("SELECT raw_format, raw FROM chatlog.chat_notifications").fetchone()
        mod = conn.execute("SELECT raw_format, raw FROM chatlog.mod_events").fetchone()
        provider = conn.execute("SELECT provider FROM chatlog.backfill_runs").fetchone()

    assert messages["m2"] == ("irc", {"line": IRC_LINE})
    # A reader can't tell a migrated row from one the writer rebuilt, so they must be the same object.
    assert messages["m1"] == ("legacy", legacy.message(FULL))
    assert messages["m3"] == ("legacy", legacy.message(PLAIN))
    assert messages["m1"][1]["message"]["fragments"][1] == {
        "type": "emote",
        "text": "Kappa",
        "emote": {"id": "25"},
    }
    assert messages["m1"][1]["reply"] == {"parent_message_id": "p1"}
    assert notice == ("legacy", legacy.notification(NOTICE))
    assert notice is not None and notice[1]["legacy"]["chatter"] is None  # the payload is kept as it was
    assert mod == ("legacy", legacy.mod_event("c1", message_id="m1", target="u1", moderator=None,
                                              duration_s=None, reason=None))  # fmt: skip
    assert provider == ("recent-messages",)

    schema.downgrade(empty_database, {"chatlog": "0001"})
    with psycopg.connect(empty_database) as conn:
        raw = conn.execute("SELECT message_id, raw FROM chatlog.messages ORDER BY 1").fetchall()
    assert raw == [("m1", None), ("m2", IRC_LINE), ("m3", None)]


def test_0006_drops_the_moved_columns_and_its_downgrade_refills_them_from_raw(empty_database: str) -> None:
    config = schema.config("chatlog", empty_database)
    command.upgrade(config, "0001")
    with psycopg.connect(empty_database) as conn:
        _insert_before_0002(conn, FULL)
        _insert_before_0002(conn, replace(PLAIN, message_id="m2"), IRC_LINE)
        conn.execute(
            "INSERT INTO chatlog.mod_events (channel_id, type, target_user_id, duration_s, reason, at)"
            " VALUES ('c1', 'timeout', 'u1', 60, 'spam', 5000)"
        )
    command.upgrade(config, "0006")
    with psycopg.connect(empty_database) as conn:
        columns = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'chatlog'"
                " AND table_name = 'messages'"
            ).fetchall()
        }
    assert "badges" not in columns and "display_name" not in columns and "raw" in columns

    command.downgrade(config, "0005")
    with psycopg.connect(empty_database) as conn:
        messages = conn.execute(
            "SELECT message_id, display_name, bits, reply_parent_id, reward_id FROM chatlog.messages ORDER BY 1"
        ).fetchall()
        mod = conn.execute("SELECT duration_s, reason FROM chatlog.mod_events").fetchone()
    # An IRC line keeps only what needs no parsing.
    assert messages == [("m1", "Alice", 100, "p1", "r1"), ("m2", None, 0, None, None)]
    assert mod == (60, "spam")


async def _rows(dbs: Databases, sql: str) -> list[tuple[Any, ...]]:
    async with await dbs.chatlog.execute(sql) as cur:
        return [tuple(r.values()) for r in await cur.fetchall()]


async def test_the_writer_stores_what_each_event_was_made_from(dbs: Databases) -> None:
    event = {"message_id": "m1", "color": "#FF0000", "message": {"text": "hi", "fragments": []}}
    writer = ChatLogWriter(dbs.chatlog)
    await writer.message(replace(FULL, raw_event=event))
    await writer.message(replace(PLAIN, raw_line=IRC_LINE, source="ivr-logs"))
    await writer.notification(NOTICE)
    await writer.moderation(MessageDeleted("c1", "m1", "u1", 5000, raw_event={"message_id": "m1"}))
    await writer.moderation(ChatCleared("c1", 6000, "ivr-logs", raw_line="@x :tmi CLEARCHAT #doomtp"))
    await writer.stop()

    assert await _rows(dbs, "SELECT message_id, raw_format, raw FROM messages ORDER BY 1") == [
        ("m1", "eventsub", event),
        ("m3", "irc", {"line": IRC_LINE}),
    ]
    assert await _rows(dbs, "SELECT raw_format, raw FROM chat_notifications") == [
        ("legacy", legacy.notification(NOTICE))
    ]
    assert await _rows(dbs, "SELECT type, raw_format, raw FROM mod_events ORDER BY at") == [
        ("delete", "eventsub", {"message_id": "m1"}),
        ("chat_clear", "irc", {"line": "@x :tmi CLEARCHAT #doomtp"}),
    ]
