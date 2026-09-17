"""Dual cooldown buckets (ADR-0006 §2): a shared bucket per tier and a personal bucket. Both must be clear."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from doomtp_bot.runtime.spec import Cooldown


@dataclass(frozen=True, slots=True)
class CooldownState:
    tier: str
    tier_remaining: int
    user_remaining: int

    @property
    def ready(self) -> bool:
        return self.tier_remaining <= 0 and self.user_remaining <= 0


class CooldownTracker:
    """In-memory buckets on a monotonic clock. They reset on restart by design."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._tier: dict[tuple[str, str, str], float] = {}
        self._user: dict[tuple[str, str, str], float] = {}

    def state(self, channel_id: str, command: str, tier: str, user_id: str | None) -> CooldownState:
        now = self._clock()
        tier_until = self._tier.get((channel_id, command, tier), 0.0)
        user_until = self._user.get((channel_id, command, user_id), 0.0) if user_id else 0.0
        return CooldownState(tier, max(0, math.ceil(tier_until - now)), max(0, math.ceil(user_until - now)))

    def commit(self, channel_id: str, command: str, tier: str, user_id: str | None, rule: Cooldown) -> None:
        now = self._clock()
        if rule.tier_s > 0:
            self._tier[(channel_id, command, tier)] = now + rule.tier_s
        if rule.user_s > 0 and user_id:
            self._user[(channel_id, command, user_id)] = now + rule.user_s
        if len(self._tier) + len(self._user) > 50_000:
            self._prune(now)

    def _prune(self, now: float) -> None:
        self._tier = {k: v for k, v in self._tier.items() if v > now}
        self._user = {k: v for k, v in self._user.items() if v > now}
