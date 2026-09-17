"""Chat log writer (append-only, batched) and the moderation index."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.core.events import Badge, ChatCleared, ChatMessage, MessageDeleted, UserMessagesCleared
from doomtp_bot.moderation.index import ModerationIndex
from doomtp_bot.storage.db import Databases


def msg(
    mid: str, user: str = "u1", login: str = "alice", text: str = "hi", at: int = 1000, channel: str = "c1"
) -> ChatMessage:
    return ChatMessage(
        message_id=mid, channel_id=channel, channel_login="doomtp", user_id=user, user_login=login,
        display_name=login.title(), text=text, sent_at=at, received_at=at + 5, badges=(Badge("subscriber", "3"),),
    )  # fmt: skip


@pytest.fixture
async def dbs(tmp_path: Path) -> AsyncIterator[Databases]:
    databases = await Databases.open(tmp_path / "bot.db", tmp_path / "chatlog.db")
    yield databases
    await databases.close()


async def rows(dbs: Databases, sql: str) -> list[tuple[object, ...]]:
    async with dbs.chatlog.execute(sql) as cur:
        return [tuple(r) for r in await cur.fetchall()]


async def test_writer_batches_and_is_idempotent(dbs: Databases) -> None:
    writer = ChatLogWriter(dbs.chatlog, flush_interval=0.01)
    writer.start()
    await writer.message(msg("m1"), is_command=True)
    await writer.message(msg("m1"))  # EventSub redelivery
    await writer.message(msg("m2", text="the doom slayer"))
    await writer.stop()
    assert await rows(dbs, "SELECT message_id, is_command, badges FROM messages ORDER BY message_id") == [
        ("m1", 1, '[{"set_id": "subscriber", "id": "3", "info": ""}]'),
        ("m2", 0, '[{"set_id": "subscriber", "id": "3", "info": ""}]'),
    ]
    assert await rows(dbs, "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'slayer'") != []


async def test_users_are_keyed_by_id_and_renames_are_kept(dbs: Databases) -> None:
    writer = ChatLogWriter(dbs.chatlog)
    await writer.message(msg("m1", login="alice", at=1000))
    await writer.message(msg("m2", login="alice_2026", at=2000))
    await writer.stop()  # not started: drains synchronously
    assert await rows(dbs, "SELECT user_id, login, first_seen, last_seen FROM users") == [
        ("u1", "alice_2026", 1000, 2000)
    ]
    assert await rows(dbs, "SELECT login FROM user_names ORDER BY seen_from") == [("alice",), ("alice_2026",)]


async def test_moderation_flags_never_delete_rows(dbs: Databases) -> None:
    writer = ChatLogWriter(dbs.chatlog)
    await writer.message(msg("m1", user="u1", at=1000))
    await writer.message(msg("m2", user="u1", at=2000))
    await writer.message(msg("m3", user="u2", login="bob", at=2500))
    await writer.moderation(MessageDeleted("c1", "m1", "u1", at=1500))
    await writer.moderation(UserMessagesCleared("c1", "u1", at=2200))
    await writer.message(msg("m4", user="u1", at=3000))  # after the timeout: not flagged
    await writer.moderation(ChatCleared("c1", at=2600))
    await writer.stop()
    assert await rows(dbs, "SELECT message_id, deleted_at, cleared_at FROM messages ORDER BY message_id") == [
        ("m1", 1500, 2200),
        ("m2", None, 2200),
        ("m3", None, 2600),
        ("m4", None, None),
    ]
    assert await rows(dbs, "SELECT type FROM mod_events ORDER BY id") == [
        ("delete",),
        ("user_clear",),
        ("chat_clear",),
    ]


async def test_sessions_runs_and_outbound(dbs: Databases) -> None:
    writer = ChatLogWriter(dbs.chatlog)
    await writer.start_session("c1")
    await writer.start_session("c1")  # idempotent while open
    await writer.command_run(
        channel_id="c1", user_id="u1", trigger_type="chat", trigger_id="m1", expr="!ping",
        resolved=[{"index": 1, "name": "ping"}], code=0, message="pong", duration_ms=3, cancelled_reason=None,
    )  # fmt: skip
    await writer.outbound(
        channel_id="c1", text_sent="pong", text_prefilter="pong", twitch_message_id="t1", dropped_reason=None
    )
    await writer.end_all_sessions("shutdown")
    await writer.stop()
    assert await rows(dbs, "SELECT channel_id, end_reason FROM log_sessions") == [("c1", "shutdown")]
    assert await rows(dbs, "SELECT expr, code FROM command_runs") == [("!ping", 0)]
    assert await rows(dbs, "SELECT text_sent, twitch_message_id FROM outbound_msgs") == [("pong", "t1")]


def test_moderation_index() -> None:
    index = ModerationIndex(clock_ms=lambda: 10_000)
    assert not index.is_invalidated("c1", "m1", "u1", 1000)
    index.record(MessageDeleted("c1", "m1", "u1", at=1100))
    assert index.is_invalidated("c1", "m1", "u1", 1000)
    index.record(UserMessagesCleared("c1", "u2", at=2000))
    assert index.is_invalidated("c1", "mX", "u2", 1999)  # sent before the timeout
    assert not index.is_invalidated("c1", "mY", "u2", 2001)  # sent after it
    assert not index.is_invalidated("c2", "mZ", "u2", 1000)  # other channel
    index.record(ChatCleared("c1", at=3000))
    assert index.checker("c1", "mQ", "u9", 2999)()
