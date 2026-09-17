"""Dispatcher: domain events → chat log, moderation index, runtime, outbox (architecture §2 main flow)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from doomtp_bot.core.events import (
    ChatCleared,
    ChatMessage,
    ChatNotification,
    Event,
    MessageDeleted,
    UserMessagesCleared,
)
from doomtp_bot.lang.ast import invocations
from doomtp_bot.lang.parser import DEFAULT_PREFIX, looks_like_command
from doomtp_bot.runtime.result import Code
from doomtp_bot.runtime.spec import LogLevel

if TYPE_CHECKING:
    from doomtp_bot.chatlog.writer import ChatLogWriter
    from doomtp_bot.core.channels import ChannelManager
    from doomtp_bot.core.outbox import Outbox
    from doomtp_bot.moderation.index import ModerationIndex
    from doomtp_bot.policy.service import PolicyService
    from doomtp_bot.runtime.engine import RunReport, Runtime

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
        max_concurrent_runs: int = MAX_CONCURRENT_RUNS,
    ) -> None:
        self.runtime = runtime
        self.policy = policy
        self.writer = writer
        self.outbox = outbox
        self.moderation = moderation
        self.channels = channels
        self._slots = asyncio.Semaphore(max_concurrent_runs)
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, event: Event) -> None:
        match event:
            case ChatMessage():
                await self._message(event)
            case ChatNotification():
                if self.channels.is_active(event.channel_id):
                    await self.writer.notification(event)
            case MessageDeleted() | UserMessagesCleared() | ChatCleared():
                self.moderation.record(event)
                if self.channels.is_active(event.channel_id):
                    await self.writer.moderation(event)

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
        if msg.is_self or not is_command:
            return
        if self.policy.is_ignored(msg.channel_id, msg.user_id) or BOT_BADGE_SET_IDS & {
            b.set_id for b in msg.badges
        }:
            return
        task = asyncio.create_task(self._run(msg), name=f"run-{msg.message_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
