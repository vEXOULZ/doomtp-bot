"""Test doubles more than one test module needs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FakeClock:
    """A monotonic clock that only moves when a test moves it."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class TickingClock:
    """Moves forward a minute on every read, so per-user cooldowns never block back-to-back test messages."""

    now: float = 0.0

    def __call__(self) -> float:
        self.now += 60.0
        return self.now
