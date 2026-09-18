"""Timers: expressions a channel runs on a clock (architecture §7).

A timer fires at most every `every_s` seconds, with optional jitter so several timers don't line up. A
timer can require the stream to be live (`only_live`) and a minimum number of chat lines since it last
fired (`min_chat_lines`), which is what keeps a quiet channel from being talked at by a bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from doomtp_bot.triggers.service import Trigger, TriggerService

if TYPE_CHECKING:
    from collections.abc import Callable

    from doomtp_bot.policy.service import PolicyService
    from doomtp_bot.triggers.runner import TriggerRunner

log = structlog.get_logger(__name__)

TICK_S = 5.0


@dataclass
class TimerState:
    next_at: float = 0.0
    lines_at_last_run: int = 0


@dataclass
class ChatActivity:
    """Lines seen per channel, so `min_chat_lines` means something."""

    counts: dict[str, int] = field(default_factory=dict)

    def saw_message(self, channel_id: str) -> None:
        self.counts[channel_id] = self.counts.get(channel_id, 0) + 1

    def lines(self, channel_id: str) -> int:
        return self.counts.get(channel_id, 0)


class TimerScheduler:
    """One asyncio task that fires due timers. Cheap: it wakes every few seconds and compares clocks."""

    def __init__(
        self,
        *,
        triggers: TriggerService,
        runner: TriggerRunner,
        policy: PolicyService,
        activity: ChatActivity,
        clock: Callable[[], float] | None = None,  # defaults to the running loop's clock
        tick_s: float = TICK_S,
        rng: random.Random | None = None,
    ) -> None:
        self.triggers = triggers
        self.runner = runner
        self.policy = policy
        self.activity = activity
        self.tick_s = tick_s
        self.rng = rng or random.Random()
        self._clock = clock
        self._state: dict[int, TimerState] = {}
        self._task: asyncio.Task[None] | None = None

    def now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="timers")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # a broken timer must not stop the others
                log.exception("timers.tick_failed")
            await asyncio.sleep(self.tick_s)

    async def tick(self) -> list[Trigger]:
        """Fire every timer that is due. Returns what ran, which makes this testable without sleeping."""
        now = self.now()
        fired: list[Trigger] = []
        for timer in self.triggers.timers():
            state = self._state.setdefault(timer.id, TimerState(next_at=now + self._interval(timer)))
            if now < state.next_at or not self._ready(timer, state):
                continue
            state.next_at = now + self._interval(timer)
            state.lines_at_last_run = self.activity.lines(timer.channel_id)
            settings = self.policy.channel_settings(timer.channel_id)
            await self.runner.run(timer, channel_login=settings.login if settings else timer.channel_id)
            fired.append(timer)
        return fired

    def _interval(self, timer: Trigger) -> float:
        jitter = float(timer.schedule.get("jitter_s") or 0)
        return timer.every_s + (self.rng.uniform(0, jitter) if jitter else 0.0)

    def _ready(self, timer: Trigger, state: TimerState) -> bool:
        settings = self.policy.channel_settings(timer.channel_id)
        if settings is None or not settings.active or settings.status != "joined":
            return False
        if timer.schedule.get("only_live") and not self._is_live(timer.channel_id):
            return False
        needed = int(timer.schedule.get("min_chat_lines") or 0)
        return self.activity.lines(timer.channel_id) - state.lines_at_last_run >= needed

    def _is_live(self, channel_id: str) -> bool:
        """Stream status arrives with the Helix poller (ADR-0007); until then, treat it as not live."""
        return bool(getattr(self.policy.channel_settings(channel_id), "live", False))
