"""Batched, append-only chat log writer (architecture §3). Never blocks command handling; never deletes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

import aiosqlite
import structlog

from doomtp_bot.core.events import (
    ChatCleared,
    ChatMessage,
    ChatNotification,
    MessageDeleted,
    ModerationAction,
    UserMessagesCleared,
)

log = structlog.get_logger(__name__)

FLUSH_INTERVAL_S = 0.5
FLUSH_BATCH = 200
QUEUE_MAX = 10_000

Op = tuple[str, tuple[Any, ...]]


def now_ms() -> int:
    return int(time.time() * 1000)


class ChatLogWriter:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        flush_interval: float = FLUSH_INTERVAL_S,
        batch: int = FLUSH_BATCH,
    ) -> None:
        self.conn = conn
        self.flush_interval = flush_interval
        self.batch = batch
        self._queue: asyncio.Queue[Op | None] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._task: asyncio.Task[None] | None = None
        self._sessions: dict[str, int] = {}
        self.last_flush_ms: int | None = None
        self.rows_written = 0

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="chatlog-writer")

    async def stop(self) -> None:
        """Drain the queue (spec: drain before closing) and stop."""
        if self._task is None:
            await self._flush_pending()
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ── producers ───────────────────────────────────────────────────────────
    async def message(self, msg: ChatMessage, *, is_command: bool = False) -> None:
        await self._put(
            "user",
            (msg.user_id, msg.user_login, msg.display_name, msg.sent_at),
        )
        await self._put(
            "message",
            (
                msg.message_id, msg.channel_id, msg.user_id, msg.user_login, msg.display_name, msg.text,
                msg.message_type, json.dumps([asdict(b) for b in msg.badges]), json.dumps(list(msg.fragments)),
                msg.bits, msg.reply_parent_id, msg.reward_id, msg.source_channel_id, int(msg.is_self),
                int(is_command), msg.source, msg.raw, msg.sent_at, msg.received_at,
            ),
        )  # fmt: skip

    async def notification(self, n: ChatNotification) -> None:
        await self._put(
            "notification",
            (n.id, n.channel_id, n.user_id, n.type, json.dumps(n.payload), n.source, n.sent_at),
        )

    async def moderation(
        self, event: MessageDeleted | UserMessagesCleared | ChatCleared | ModerationAction
    ) -> None:
        match event:
            case MessageDeleted():
                await self._put(
                    "mod_event",
                    (
                        event.channel_id,
                        "delete",
                        event.message_id,
                        event.target_user_id,
                        None,
                        None,
                        None,
                        event.source,
                        event.at,
                    ),
                )
                await self._put("flag_deleted", (event.at, event.message_id))
            case UserMessagesCleared():
                await self._put(
                    "mod_event",
                    (
                        event.channel_id,
                        "user_clear",
                        None,
                        event.target_user_id,
                        None,
                        None,
                        None,
                        event.source,
                        event.at,
                    ),
                )
                await self._put(
                    "flag_user_cleared", (event.at, event.channel_id, event.target_user_id, event.at)
                )
            case ChatCleared():
                await self._put(
                    "mod_event",
                    (event.channel_id, "chat_clear", None, None, None, None, None, event.source, event.at),
                )
                await self._put("flag_chat_cleared", (event.at, event.channel_id, event.at))
            case ModerationAction():
                kind = event.action if event.action in ("timeout", "ban", "unban", "delete") else "user_clear"
                await self._put("mod_event", (event.channel_id, kind, event.extra.get("message_id"), event.target_user_id,
                                              event.moderator_user_id, event.duration_s, event.reason, "eventsub", event.at))  # fmt: skip

    async def command_run(
        self,
        *,
        channel_id: str,
        user_id: str | None,
        trigger_type: str,
        trigger_id: str | None,
        expr: str,
        resolved: Sequence[dict[str, Any]],
        code: int,
        message: str | None,
        duration_ms: int,
        cancelled_reason: str | None,
    ) -> None:
        await self._put(
            "command_run",
            (channel_id, user_id, trigger_type, trigger_id, expr, json.dumps(list(resolved)), code, message,
             duration_ms, cancelled_reason, now_ms()),
        )  # fmt: skip

    async def outbound(
        self,
        *,
        channel_id: str,
        text_sent: str | None,
        text_prefilter: str | None,
        twitch_message_id: str | None,
        dropped_reason: str | None,
        filter_hits: Sequence[str] = (),
    ) -> None:
        await self._put(
            "outbound",
            (
                channel_id,
                text_sent,
                text_prefilter,
                json.dumps(list(filter_hits)),
                twitch_message_id,
                dropped_reason,
                now_ms(),
            ),
        )

    # ── sessions (written immediately, not batched) ─────────────────────────
    async def start_session(self, channel_id: str) -> None:
        if channel_id in self._sessions:
            return
        cur = await self.conn.execute(
            "INSERT INTO log_sessions (channel_id, started_at) VALUES (?, ?)", (channel_id, now_ms())
        )
        await self.conn.commit()
        self._sessions[channel_id] = int(cur.lastrowid or 0)

    async def end_session(self, channel_id: str, reason: str) -> None:
        session_id = self._sessions.pop(channel_id, None)
        if session_id is None:
            return
        await self.conn.execute(
            "UPDATE log_sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (now_ms(), reason, session_id),
        )
        await self.conn.commit()

    async def end_all_sessions(self, reason: str) -> None:
        for channel_id in list(self._sessions):
            await self.end_session(channel_id, reason)

    # ── internals ───────────────────────────────────────────────────────────
    async def _put(self, kind: str, params: tuple[Any, ...]) -> None:
        if self._queue.full():
            log.warning(
                "chatlog.queue_full", depth=self._queue.qsize()
            )  # blocks the producer rather than dropping
        await self._queue.put((kind, params))

    async def _run(self) -> None:
        stopping = False
        while not stopping:
            pending: list[Op] = []
            first = await self._queue.get()
            if first is None:
                stopping = True
            else:
                pending.append(first)
                deadline = asyncio.get_running_loop().time() + self.flush_interval
                while len(pending) < self.batch:
                    timeout = deadline - asyncio.get_running_loop().time()
                    if timeout <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(self._queue.get(), timeout)
                    except TimeoutError:
                        break
                    if item is None:
                        stopping = True
                        break
                    pending.append(item)
            if stopping:
                with contextlib.suppress(asyncio.QueueEmpty):
                    while True:
                        item = self._queue.get_nowait()
                        if item is not None:
                            pending.append(item)
            if pending:
                await self._write(pending)

    async def _flush_pending(self) -> None:
        pending: list[Op] = []
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                item = self._queue.get_nowait()
                if item is not None:
                    pending.append(item)
        if pending:
            await self._write(pending)

    async def _write(self, ops: list[Op]) -> None:
        try:
            for kind, params in ops:
                await self.conn.execute(_SQL[kind], params)
                if kind == "user":
                    await self.conn.execute(_SQL["user_name"], (params[0], params[1], params[2], params[3]))
            await self.conn.commit()
            self.rows_written += len(ops)
            self.last_flush_ms = now_ms()
        except Exception:
            await self.conn.rollback()
            log.exception("chatlog.flush_failed", ops=len(ops))


_SQL: dict[str, str] = {
    "user": (
        "INSERT INTO users (user_id, login, display_name, first_seen, last_seen) VALUES (?1, ?2, ?3, ?4, ?4)"
        " ON CONFLICT (user_id) DO UPDATE SET login = excluded.login, display_name = excluded.display_name,"
        " last_seen = MAX(users.last_seen, excluded.last_seen)"
    ),
    "user_name": "INSERT OR IGNORE INTO user_names (user_id, login, display_name, seen_from) VALUES (?, ?, ?, ?)",
    "message": (
        "INSERT OR IGNORE INTO messages (message_id, channel_id, user_id, user_login, display_name, text, message_type,"
        " badges, fragments, bits, reply_parent_id, reward_id, source_channel_id, is_self, is_command, source, raw,"
        " sent_at, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ),
    "notification": (
        "INSERT OR IGNORE INTO chat_notifications (id, channel_id, user_id, type, payload, source, sent_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
    ),
    "mod_event": (
        "INSERT INTO mod_events (channel_id, type, message_id, target_user_id, moderator_user_id, duration_s, reason,"
        " source, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ),
    "flag_deleted": "UPDATE messages SET deleted_at = COALESCE(deleted_at, ?) WHERE message_id = ?",
    "flag_user_cleared": (
        "UPDATE messages SET cleared_at = ? WHERE channel_id = ? AND user_id = ? AND sent_at <= ? AND cleared_at IS NULL"
    ),
    "flag_chat_cleared": "UPDATE messages SET cleared_at = ? WHERE channel_id = ? AND sent_at <= ? AND cleared_at IS NULL",
    "command_run": (
        "INSERT INTO command_runs (channel_id, user_id, trigger_type, trigger_id, expr, resolved, code, message,"
        " duration_ms, cancelled_reason, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ),
    "outbound": (
        "INSERT INTO outbound_msgs (channel_id, text_sent, text_prefilter, filter_hits, twitch_message_id,"
        " dropped_reason, at) VALUES (?, ?, ?, ?, ?, ?, ?)"
    ),
}
