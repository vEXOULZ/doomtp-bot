"""Test doubles more than one test module needs."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.storage.db import Connection

SETUP = Actor(None, "system")


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


async def policy_with_channels(
    conn: Connection,
    *channels: tuple[str, str],
    joined: bool = False,
    bot_owner_ids: frozenset[str] = frozenset(),
    clock: Callable[[], float] = time.monotonic,
) -> PolicyService:
    """A loaded PolicyService on the `bot` database, knowing each (channel_id, login) given.

    `joined` also marks them joined, as a live `!join` would; otherwise they are only known.
    """
    policy = PolicyService(conn, bot_owner_ids=bot_owner_ids, clock=clock)
    await policy.reload()
    for channel_id, login in channels:
        await policy.mutate(lambda repo, c=channel_id, n=login: repo.ensure_channel(c, n, SETUP))
        if joined:
            await policy.mutate(
                lambda repo, c=channel_id: repo.set_channel_field(c, "status", "joined", SETUP)
            )
    return policy
