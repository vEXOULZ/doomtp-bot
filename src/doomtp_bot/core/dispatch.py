"""Dispatcher: domain events → chat log, moderation index, runtime, outbox (architecture §2 main flow)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from doomtp_bot.core.events import (
    ChatCleared,
    ChatMessage,
    ChatNotification,
    Event,
    MessageDeleted,
    StreamStatusChanged,
    UserMessagesCleared,
)
from doomtp_bot.lang.ast import invocations
from doomtp_bot.lang.parser import DEFAULT_PREFIX, looks_like_command
from doomtp_bot.runtime.result import Code
from doomtp_bot.runtime.spec import LogLevel

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from doomtp_bot.chatlog.writer import ChatLogWriter
    from doomtp_bot.core.channels import ChannelManager
    from doomtp_bot.core.outbox import Outbox
    from doomtp_bot.core.streams import StreamStatus
    from doomtp_bot.moderation.index import ModerationIndex
    from doomtp_bot.policy.service import PolicyService
    from doomtp_bot.runtime.engine import RunReport, Runtime
    from doomtp_bot.triggers.runner import TriggerRunner
    from doomtp_bot.triggers.service import TriggerService
    from doomtp_bot.triggers.timers import ChatActivity

log = structlog.get_logger(__name__)

# Twitch's "Chat Bot" badge. Messages from other verified bots never trigger commands. (Verify set_id live.)
BOT_BADGE_SET_IDS = frozenset({"bot-badge"})
MAX_CONCURRENT_RUNS = 50


def should_log_run(report: RunReport, level: LogLevel) -> bool:
    """Per-command log level (architecture §4.5)."""
    code = report.result.code
    if level is LogLevel.OFF:
        return False
    if level is LogLevel.ALL:
        return True
    if code in (Code.UNKNOWN, Code.COOLDOWN, Code.DENIED):
        return False
    if level is LogLevel.ERRORS:
        return code != Code.OK
    if level is LogLevel.OUTPUT:
        return code != Code.OK or bool(report.send) or bool(report.committed)
    return True


class Dispatcher:
    def __init__(
        self,
        *,
        runtime: Runtime,
        policy: PolicyService,
        writer: ChatLogWriter,
        outbox: Outbox,
        moderation: ModerationIndex,
        channels: ChannelManager,
        triggers: TriggerService | None = None,
        trigger_runner: TriggerRunner | None = None,
        activity: ChatActivity | None = None,
        streams: StreamStatus | None = None,
        max_concurrent_runs: int = MAX_CONCURRENT_RUNS,
    ) -> None:
        self.runtime = runtime
        self.policy = policy
        self.writer = writer
        self.outbox = outbox
        self.moderation = moderation
        self.channels = channels
        self.triggers = triggers
        self.trigger_runner = trigger_runner
        self.activity = activity
        self.streams = streams
        self._slots = asyncio.Semaphore(max_concurrent_runs)
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, event: Event) -> None:
        match event:
            case ChatMessage():
                await self._message(event)
            case ChatNotification():
                if self.channels.is_active(event.channel_id):
                    await self.writer.notification(event)
                    self._spawn(self._notification_triggers(event), f"trigger-{event.id}")
            case MessageDeleted() | UserMessagesCleared() | ChatCleared():
                self.moderation.record(event)
                if self.channels.is_active(event.channel_id):
                    await self.writer.moderation(event)
            case StreamStatusChanged():
                if self.channels.is_active(event.channel_id):
                    self._spawn(self._stream_triggers(event), f"stream-{event.channel_id}")

    async def _message(self, msg: ChatMessage) -> None:
        if not self.channels.is_active(msg.channel_id):
            return
        settings = self.policy.channel_settings(msg.channel_id)
        prefix = settings.prefix if settings else DEFAULT_PREFIX
        is_command = looks_like_command(msg.text, prefix, msg.reply_mentions)
        if settings is None or settings.log_enabled:
            await self.writer.message(
                msg, is_command=is_command
            )  # logged first, always — including ignored users
        if self.activity is not None and not msg.is_self:
            self.activity.saw_message(msg.channel_id)
        if msg.is_self:
            return
        if self.policy.is_ignored(msg.channel_id, msg.user_id) or BOT_BADGE_SET_IDS & {
            b.set_id for b in msg.badges
        }:
            return
        if not is_command:
            self._spawn(self._listeners(msg), f"listen-{msg.message_id}")  # architecture §7
            return
        self._spawn(self._run(msg), f"run-{msg.message_id}")

    def _spawn(self, work: Coroutine[None, None, None], name: str) -> None:
        task = asyncio.create_task(work, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _listeners(self, msg: ChatMessage) -> None:
        """Regex listeners run on ordinary chat lines, cancelled by moderation like any other run."""
        if self.triggers is None or self.trigger_runner is None:
            return
        hits = self.triggers.listeners_matching(msg.channel_id, msg.text)
        if not hits:
            return
        invalidated = self.moderation.checker(msg.channel_id, msg.message_id, msg.user_id, msg.sent_at)
        async with self._slots:
            for trigger, fields in hits:
                await self.trigger_runner.run(
                    trigger,
                    channel_login=msg.channel_login,
                    event={"user": {"id": msg.user_id, "name": msg.user_login}, "message": msg.text},
                    match=fields,
                    user=(msg.user_id, msg.user_login, msg.display_name),
                    input_text=msg.text,
                    is_cancelled=invalidated,
                    message_id=msg.message_id,
                )

    async def _stream_triggers(self, event: StreamStatusChanged) -> None:
        """The Helix poller saw the stream go up or down (ADR-0007)."""
        if self.triggers is None or self.trigger_runner is None:
            return
        type_ = "stream_online" if event.live else "stream_offline"
        settings = self.policy.channel_settings(event.channel_id)
        login = settings.login if settings else event.channel_id
        payload: dict[str, Any] = {"live": event.live, "channel": {"login": login}}
        if self.streams is not None:
            payload.update(self.streams.info(event.channel_id))
        found = self.triggers.event_triggers(event.channel_id, type_, payload)
        async with self._slots:
            for trigger in found:
                await self.trigger_runner.run(trigger, channel_login=login, event=payload)

    async def _notification_triggers(self, event: ChatNotification) -> None:
        """Subs, resubs, gift subs, raids and announcements (architecture §7)."""
        if self.triggers is None or self.trigger_runner is None:
            return
        found = self.triggers.event_triggers(event.channel_id, event.type, event.payload)
        if not found:
            return
        settings = self.policy.channel_settings(event.channel_id)
        user = event.payload.get("user") or event.payload.get("chatter") or {}
        async with self._slots:
            for trigger in found:
                await self.trigger_runner.run(
                    trigger,
                    channel_login=settings.login if settings else event.channel_id,
                    event=event.payload,
                    user=(
                        (
                            event.user_id,
                            str(user.get("name") or user.get("login") or ""),
                            str(user.get("display") or user.get("name") or ""),
                        )
                        if event.user_id
                        else None
                    ),
                    input_text=str(event.payload.get("message") or ""),
                )

    async def drain(self) -> None:
        """Wait for in-flight command runs (used at shutdown and in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, msg: ChatMessage) -> None:
        async with self._slots:
            try:
                chatter = self.policy.build_chatter(
                    msg.channel_id,
                    msg.user_id,
                    msg.user_login,
                    msg.display_name,
                    frozenset(b.set_id for b in msg.badges),
                )
                channel = self.policy.channel_info(msg.channel_id, msg.channel_login)
                invalidated = self.moderation.checker(
                    msg.channel_id, msg.message_id, msg.user_id, msg.sent_at
                )
                ctx = self.runtime.make_context(
                    channel=channel,
                    invoker=chatter,
                    trigger_type="chat",
                    message_id=msg.message_id,
                    message_sent_at=msg.sent_at / 1000,
                    is_cancelled=invalidated,
                )
                report = await self.runtime.run(msg.text, ctx, reply_parent_login=msg.reply_mentions)
                if report is None:
                    return
                await self._log_run(msg, report, ctx.run_id)
                if report.send:
                    await self.outbox.send(
                        msg.channel_id,
                        report.send,
                        reply_to=msg.message_id,
                        is_invalidated=invalidated,
                        run_ref=ctx.run_id,
                    )
            except Exception:
                log.exception("dispatch.run_failed", message_id=msg.message_id)

    async def _log_run(self, msg: ChatMessage, report: RunReport, run_ref: str) -> None:
        level = LogLevel.INVOCATIONS
        resolved: list[dict[str, object]] = []
        if report.ast is not None:
            invs = invocations(report.ast)
            for inv in invs:
                resolved.append({"index": inv.index, "name": inv.name, "personal": inv.personal})
            first = self.runtime.registry.get(invs[0].name)
            if first is not None:
                level = self.policy.log_level(msg.channel_id, first.spec)
        if not should_log_run(report, level):
            return
        await self.writer.command_run(
            channel_id=msg.channel_id,
            user_id=msg.user_id,
            trigger_type="chat",
            trigger_id=msg.message_id,
            expr=report.expr,
            resolved=resolved,
            code=report.result.code,
            message=report.result.message,
            duration_ms=report.duration_ms,
            cancelled_reason="moderated" if report.cancelled else None,
            run_ref=run_ref,
        )
