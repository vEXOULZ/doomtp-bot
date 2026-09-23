"""Running a trigger's expression (architecture §7).

Every trigger goes through the same runtime a typed command does, at the rank its creator chose, with
the event payload in `{event.*}` and listener captures in `{match.*}`. The chatter is the event's user —
the raider, the subscriber, the person who matched — and a timer has none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from doomtp_bot.lang.parser import Context
from doomtp_bot.runtime.executor import ScopeArgs
from doomtp_bot.triggers.service import Trigger

if TYPE_CHECKING:
    from collections.abc import Callable

    from doomtp_bot.core.outbox import Outbox
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
    ) -> None:
        self.runtime = runtime
        self.policy = policy
        self.outbox = outbox

    async def run(
        self,
        trigger: Trigger,
        *,
        channel_login: str,
        event: dict[str, Any] | None = None,
        match: dict[str, Any] | None = None,
        user: tuple[str, str, str] | None = None,  # (id, login, display)
        input_text: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        message_id: str | None = None,
    ) -> RunReport | None:
        """Run one trigger and send whatever it produced. Returns the report, or None if it didn't parse."""
        channel = self.policy.channel_info(trigger.channel_id, channel_login)
        chatter = None
        if user is not None:
            chatter = self.policy.build_chatter(trigger.channel_id, user[0], user[1], user[2])
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
        log.info(
            "trigger.ran",
            trigger=trigger.id,
            type=trigger.type,
            channel=trigger.channel_id,
            code=report.result.code,
        )
        return report
