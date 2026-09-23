"""Batched, append-only chat log writer (architecture §3). Never blocks command handling; never deletes."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

import structlog

from doomtp_bot.clock import now_ms
from doomtp_bot.core import metrics
from doomtp_bot.core.events import (
    ChatCleared,
    ChatMessage,
    ChatNotification,
    MessageDeleted,
    ModerationAction,
    UserMessagesCleared,
)
from doomtp_bot.storage.db import Connection, fetch_value, transaction

log = structlog.get_logger(__name__)

FLUSH_INTERVAL_S = 0.5
FLUSH_BATCH = 200
QUEUE_MAX = 10_000

Op = tuple[str, tuple[Any, ...]]
_MESSAGE_SOURCE = 15  # where `source` sits in a "message" op's parameters


class ChatLogWriter:
    def __init__(
        self,
        conn: Connection,
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
        user = (msg.user_id, msg.user_login, msg.display_name, msg.sent_at)
        await self._put("user", (*user, msg.sent_at))
        await self._put("user_name", user)
        await self._put(
            "message",
            (
                msg.message_id, msg.channel_id, msg.user_id, msg.user_login, msg.display_name, msg.text,
                msg.message_type, json.dumps([asdict(b) for b in msg.badges]), json.dumps(list(msg.fragments)),
                msg.bits, msg.reply_parent_id, msg.reward_id, msg.source_channel_id, msg.is_self,
                is_command, msg.source, msg.raw, msg.sent_at, msg.received_at,
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
                await self._mod_event(event.channel_id, "delete", event.source, event.at,
                                      message_id=event.message_id, target=event.target_user_id)  # fmt: skip
                await self._put("flag_deleted", (event.at, event.message_id))
            case UserMessagesCleared():
                await self._mod_event(
                    event.channel_id, "user_clear", event.source, event.at, target=event.target_user_id
                )
                await self._put(
                    "flag_user_cleared", (event.at, event.channel_id, event.target_user_id, event.at)
                )
            case ChatCleared():
                await self._mod_event(event.channel_id, "chat_clear", event.source, event.at)
                await self._put("flag_chat_cleared", (event.at, event.channel_id, event.at))
            case ModerationAction():
                kind = event.action if event.action in ("timeout", "ban", "unban", "delete") else "user_clear"
                await self._mod_event(event.channel_id, kind, "eventsub", event.at,
                                      message_id=event.extra.get("message_id"), target=event.target_user_id,
                                      moderator=event.moderator_user_id, duration_s=event.duration_s,
                                      reason=event.reason)  # fmt: skip

    async def _mod_event(
        self,
        channel_id: str,
        kind: str,
        source: str,
        at: int,
        *,
        message_id: str | None = None,
        target: str | None = None,
        moderator: str | None = None,
        duration_s: int | None = None,
        reason: str | None = None,
    ) -> None:
        await self._put(
            "mod_event", (channel_id, kind, message_id, target, moderator, duration_s, reason, source, at)
        )

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
        run_ref: str | None = None,
    ) -> None:
        await self._put(
            "command_run",
            (channel_id, user_id, trigger_type, trigger_id, expr, json.dumps(list(resolved)), code, message,
             duration_ms, cancelled_reason, run_ref, now_ms()),
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
        run_ref: str | None = None,
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
                run_ref,
                now_ms(),
            ),
        )

    # ── sessions (written immediately, not batched) ─────────────────────────
    async def start_session(self, channel_id: str) -> None:
        if channel_id in self._sessions:
            return
        async with transaction(self.conn):
            session_id = await fetch_value(
                self.conn,
                "INSERT INTO log_sessions (channel_id, started_at) VALUES (%s, %s) RETURNING id",
                (channel_id, now_ms()),
            )
        self._sessions[channel_id] = int(session_id or 0)

    async def end_session(self, channel_id: str, reason: str) -> None:
        session_id = self._sessions.pop(channel_id, None)
        if session_id is None:
            return
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE log_sessions SET ended_at = %s, end_reason = %s WHERE id = %s",
                (now_ms(), reason, session_id),
            )

    async def close_stale_sessions(self) -> int:
        """Close sessions a previous process never ended (killed or crashed). Call once at startup.

        The end time is the last message received in that channel before the next session began, or the
        session start if none was logged, so coverage gaps stay honest (ADR-0008).
        """
        async with transaction(self.conn):
            cur = await self.conn.execute(
                """
            UPDATE log_sessions SET end_reason = 'unclean_shutdown', ended_at = COALESCE(
                (SELECT MAX(m.received_at) FROM messages m
                  WHERE m.channel_id = log_sessions.channel_id
                    AND m.received_at >= log_sessions.started_at
                    AND m.received_at < COALESCE(
                        (SELECT MIN(s2.started_at) FROM log_sessions s2
                          WHERE s2.channel_id = log_sessions.channel_id AND s2.started_at > log_sessions.started_at),
                        9223372036854775807)),
                started_at)
            WHERE ended_at IS NULL
            """
            )
        return cur.rowcount or 0

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
            first = await self._queue.get()
            pending: list[Op] = []
            if first is None:
                stopping = True
            else:
                pending.append(first)
                loop = asyncio.get_running_loop()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout_at(loop.time() + self.flush_interval):
                        while len(pending) < self.batch:
                            item = await self._queue.get()
                            if item is None:
                                stopping = True
                                break
                            pending.append(item)
            if stopping:
                pending.extend(self._drain_nowait())
            if pending:
                await self._write(pending)

    async def _flush_pending(self) -> None:
        pending = self._drain_nowait()
        if pending:
            await self._write(pending)

    def _drain_nowait(self) -> list[Op]:
        drained: list[Op] = []
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                item = self._queue.get_nowait()
                if item is not None:
                    drained.append(item)
        return drained

    async def _write(self, ops: list[Op]) -> None:
        try:
            async with transaction(self.conn):
                logged = [source for op in ops if (source := await self._execute(op)) is not None]
        except Exception:
            log.exception("chatlog.flush_failed", ops=len(ops))
            await self._write_one_by_one(ops)  # one bad row must not lose the whole batch
        else:
            for source in logged:  # counted once the batch is committed, not before
                metrics.MESSAGES_LOGGED.inc(source=source)
        self.last_flush_ms = now_ms()

    async def _write_one_by_one(self, ops: list[Op]) -> None:
        for op in ops:
            try:
                async with transaction(self.conn):
                    source = await self._execute(op)
            except Exception:
                log.exception("chatlog.row_dropped", kind=op[0])
            else:
                if source is not None:
                    metrics.MESSAGES_LOGGED.inc(source=source)

    async def _execute(self, op: Op) -> str | None:
        """Run one op. For a message Postgres actually inserted, its source; a re-offered one isn't new."""
        kind, params = op
        cursor = await self.conn.execute(_SQL[kind], params)
        if kind == "message" and cursor.rowcount == 1:
            return str(params[_MESSAGE_SOURCE])
        return None


_SQL: dict[str, str] = {
    "user": (
        "INSERT INTO users (user_id, login, display_name, first_seen, last_seen) VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (user_id) DO UPDATE SET login = EXCLUDED.login, display_name = EXCLUDED.display_name,"
        # GREATEST, not MAX: MAX takes two arguments in SQLite but is an aggregate in Postgres.
        " last_seen = GREATEST(users.last_seen, EXCLUDED.last_seen)"
    ),
    "user_name": (
        "INSERT INTO user_names (user_id, login, display_name, seen_from) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT DO NOTHING"
    ),
    "message": (
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, display_name, text, message_type,"
        " badges, fragments, bits, reply_parent_id, reward_id, source_channel_id, is_self, is_command, source, raw,"
        " sent_at, received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        # Backfill re-offers messages EventSub already logged; the first one wins (ADR-0008).
        " ON CONFLICT DO NOTHING"
    ),
    "notification": (
        "INSERT INTO chat_notifications (id, channel_id, user_id, type, payload, source, sent_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
    ),
    "mod_event": (
        "INSERT INTO mod_events (channel_id, type, message_id, target_user_id, moderator_user_id, duration_s, reason,"
        " source, at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    ),
    "flag_deleted": "UPDATE messages SET deleted_at = COALESCE(deleted_at, %s) WHERE message_id = %s",
    "flag_user_cleared": (
        "UPDATE messages SET cleared_at = %s WHERE channel_id = %s AND user_id = %s AND sent_at <= %s AND cleared_at IS NULL"
    ),
    "flag_chat_cleared": "UPDATE messages SET cleared_at = %s WHERE channel_id = %s AND sent_at <= %s AND cleared_at IS NULL",
    "command_run": (
        "INSERT INTO command_runs (channel_id, user_id, trigger_type, trigger_id, expr, resolved, code, message,"
        " duration_ms, cancelled_reason, run_ref, at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    ),
    "outbound": (
        "INSERT INTO outbound_msgs (channel_id, text_sent, text_prefilter, filter_hits, twitch_message_id,"
        " dropped_reason, run_ref, at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    ),
}
