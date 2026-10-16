"""Running a trigger's expression (architecture §7).

Every trigger goes through the same runtime a typed command does, at the rank its creator chose, with
the event payload in `{event.*}` and listener captures in `{match.*}`. The chatter is the event's user —
the raider, the subscriber, the person who matched — and a timer has none.

A trigger's own cooldowns (ADR-0031) hold it back before it runs: one window for the whole channel and one
per chatter, kept in memory like a command's buckets. A firing inside either is skipped quietly.

Each run is written to the run log (`command_runs`) as a chat run is, under the trigger's own log level, so
the channel's Runs page shows what its triggers did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from doomtp_bot.core.dispatch import should_log_run
from doomtp_bot.core.streams import live_fields
from doomtp_bot.lang.ast import invocations
from doomtp_bot.lang.parser import Context
from doomtp_bot.policy.cooldowns import CooldownTracker
from doomtp_bot.runtime.executor import ScopeArgs
from doomtp_bot.runtime.spec import Cooldown
from doomtp_bot.triggers.service import MIN_CHANNEL_COOLDOWN_S, Trigger

if TYPE_CHECKING:
    from collections.abc import Callable

    from doomtp_bot.chatlog.writer import ChatLogWriter
    from doomtp_bot.core.outbox import Outbox
    from doomtp_bot.core.streams import StreamStatus
    from doomtp_bot.policy.service import PolicyService
    from doomtp_bot.runtime.engine import RunReport, Runtime

log = structlog.get_logger(__name__)


class TriggerRunner:
    def __init__(
        self,
        *,
        runtime: Runtime,
        policy: PolicyService,
        outbox: Outbox,
        streams: StreamStatus | None = None,
        cooldowns: CooldownTracker | None = None,
        writer: ChatLogWriter | None = None,
    ) -> None:
        self.runtime = runtime
        self.writer = writer
        self.policy = policy
        self.outbox = outbox
        self.streams = streams
        self.cooldowns = cooldowns or CooldownTracker()

    @staticmethod
    def channel_cooldown_s(trigger: Trigger) -> int:
        """Its whole-channel cooldown, at least the least its type runs with (ADR-0034)."""
        return max(trigger.cooldown_s, MIN_CHANNEL_COOLDOWN_S.get(trigger.type, 0))

    def cooling(self, trigger: Trigger, user_id: str | None) -> bool:
        """Whether one of the trigger's cooldowns still holds it, here or for this chatter (ADR-0031). A pack
        trigger shares its id across channels, so the channel is part of the key."""
        if not self.channel_cooldown_s(trigger) and not trigger.user_cooldown_s:
            return False
        state = self.cooldowns.state(trigger.channel_id, f"trigger:{trigger.id}", "trigger", user_id)
        return not state.ready

    async def run(
        self,
        trigger: Trigger,
        *,
        channel_login: str,
        event: dict[str, Any] | None = None,
        match: dict[str, Any] | None = None,
        user: tuple[str, str, str] | None = None,  # (id, login, display)
        badges: frozenset[str] = frozenset(),  # the user's badge set ids, for `$chatter.rank`
        input_text: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        message_id: str | None = None,
    ) -> RunReport | None:
        """Run one trigger and send whatever it produced. Returns the report, or None if it didn't parse or
        is still cooling down."""
        user_id = user[0] if user is not None else None
        if self.cooling(trigger, user_id):
            log.info("trigger.cooldown", trigger=trigger.id, type=trigger.type, channel=trigger.channel_id)
            return None
        channel_s = self.channel_cooldown_s(trigger)
        if channel_s or trigger.user_cooldown_s:
            # Taken before the run, with no await in between, so two lines arriving together can't both
            # get through. A run that then fails or is moderated away has still used its turn.
            rule = Cooldown(tier_s=channel_s, user_s=trigger.user_cooldown_s)
            self.cooldowns.commit(trigger.channel_id, f"trigger:{trigger.id}", "trigger", user_id, rule)
        channel = self.policy.channel_info(
            trigger.channel_id, channel_login, **live_fields(self.streams, trigger.channel_id)
        )
        chatter = None
        if user is not None:
            chatter = self.policy.build_chatter(trigger.channel_id, user[0], user[1], user[2], badges)
        context = Context.LISTENER if trigger.type == "listener" else Context.TRIGGER
        ctx = self.runtime.make_context(
            channel=channel,
            invoker=chatter,
            context=context,
            trigger_type=trigger.type,
            trigger_id=str(trigger.id),
            message_id=message_id,
            run_as_rank=trigger.run_as_rank,
            event={"type": trigger.type, **(event or {})},
            match=match or {},
            is_cancelled=is_cancelled or (lambda: False),
        )
        report = await self.runtime.run(trigger.expr, ctx, scope_args=ScopeArgs.from_text(input_text or ""))
        if report is None:
            return None
        if report.send:
            await self.outbox.send(
                trigger.channel_id,
                report.send,
                is_invalidated=is_cancelled or (lambda: False),
                run_ref=ctx.run_id,
            )
        await self._log_run(trigger, user_id, report, ctx.run_id)
        log.info(
            "trigger.ran",
            trigger=trigger.id,
            type=trigger.type,
            channel=trigger.channel_id,
            code=report.result.code,
        )
        return report

    async def _log_run(self, trigger: Trigger, user_id: str | None, report: RunReport, run_ref: str) -> None:
        """One `command_runs` row, typed by the trigger's type, if its log level keeps this run."""
        if self.writer is None or not should_log_run(report, trigger.log_level):
            return
        resolved: list[dict[str, object]] = []
        if report.ast is not None:
            resolved = [{"index": i.index, "name": i.name, "personal": i.personal} for i in invocations(report.ast)]
        await self.writer.command_run(
            channel_id=trigger.channel_id,
            user_id=user_id,
            trigger_type=trigger.type,
            trigger_id=str(trigger.id),
            expr=report.expr,
            resolved=resolved,
            code=report.result.code,
            message=report.result.message,
            duration_ms=report.duration_ms,
            cancelled_reason="moderated" if report.cancelled else None,
            run_ref=run_ref,
        )
