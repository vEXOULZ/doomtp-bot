"""What the bot is allowed to do in a channel, and how it finds out (ADR-0007, architecture §10).

A channel gives the bot one of three tiers: `basic` (anybody can add the bot, chat only), `moderator`
(the broadcaster modded it) and `full` (the broadcaster signed in). Nothing tells the bot which one it
has, so the probe measures it: it asks for a subscription only a moderator may hold, and reads the
answer. Success is the proof — there is no "am I a mod here" endpoint the bot's own token can call
without a scope the broadcaster would have to grant anyway.

The result lands in `channels.capabilities`, which is what `CommandSpec.requires` and trigger types are
checked against, so an unavailable feature says *why* instead of failing at the Twitch API.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from doomtp_bot.policy.repository import Actor, PolicyRepository

if TYPE_CHECKING:
    from doomtp_bot.core.channels import ChannelManager
    from doomtp_bot.policy.service import PolicyService

log = structlog.get_logger(__name__)

# Capability names. Specs and triggers declare these; the probe decides which ones a channel has.
CHAT = "chat"  # read and send — every joined channel
FOLLOWERS = "followers"  # channel.follow events
MODERATE = "moderate"  # delete, timeout and ban as the bot
REDEMPTIONS = "redemptions"  # channel point redemptions (needs the broadcaster's token)
SUBS = "subs"  # subscription events with details
BITS = "bits"  # cheer events with details

MODERATOR_CAPABILITIES = frozenset({FOLLOWERS, MODERATE})
FULL_CAPABILITIES = frozenset({REDEMPTIONS, SUBS, BITS})
# What each broadcaster scope buys, once they have connected their channel (ADR-0007 item 5).
CAPABILITY_SCOPES = {
    REDEMPTIONS: ("channel:read:redemptions", "channel:manage:redemptions"),
    SUBS: ("channel:read:subscriptions",),
    BITS: ("bits:read",),
}
INTERVAL_S = 3600.0  # hourly, per ADR-0007


def granted_by(scopes: tuple[str, ...] | list[str] | set[str]) -> frozenset[str]:
    """The capabilities a broadcaster's granted scopes add. Partial grants are normal, and fine."""
    held = set(scopes)
    return frozenset(capability for capability, wanted in CAPABILITY_SCOPES.items() if held & set(wanted))


def tier_for(capabilities: frozenset[str] | set[str]) -> str:
    if FULL_CAPABILITIES & set(capabilities):
        return "full"
    if MODERATOR_CAPABILITIES & set(capabilities):
        return "moderator"
    return "basic"


class Prober(Protocol):
    async def try_moderator_subscription(self, channel_id: str) -> bool:
        """True if the bot may hold a moderator-only subscription in that channel."""
        ...


class CapabilityProbe:
    """Runs at join, hourly, and whenever Twitch answers a request with 401 or 403."""

    def __init__(
        self,
        *,
        policy: PolicyService,
        channels: ChannelManager,
        prober: Prober | None,
        interval_s: float = INTERVAL_S,
    ) -> None:
        self.policy = policy
        self.channels = channels
        self.prober = prober
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="capability-probe")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.probe_all()
            except Exception:
                log.exception("capabilities.probe_failed")

    async def probe_all(self) -> dict[str, frozenset[str]]:
        found = {}
        for settings in self.channels.active_channels():
            found[settings.channel_id] = await self.probe(settings.channel_id)
        return found

    async def probe(self, channel_id: str) -> frozenset[str]:
        """Measure one channel and store the result if it moved."""
        capabilities = {CHAT}
        if self.prober is not None and await self.prober.try_moderator_subscription(channel_id):
            capabilities |= MODERATOR_CAPABILITIES
        settings = self.policy.channel_settings(channel_id)
        if settings is not None:
            # The broadcaster's own grants are not something this probe can take away (ADR-0007 item 5).
            capabilities |= set(settings.capabilities) & FULL_CAPABILITIES
        await self._store(channel_id, frozenset(capabilities))
        return frozenset(capabilities)

    async def grant(self, channel_id: str, capabilities: frozenset[str]) -> frozenset[str]:
        """Add what a broadcaster just granted, keeping whatever the probe already found."""
        settings = self.policy.channel_settings(channel_id)
        held = set(settings.capabilities) if settings is not None else {CHAT}
        merged = frozenset(held | set(capabilities) | {CHAT})
        await self._store(channel_id, merged)
        return merged

    async def revoke_full(self, channel_id: str) -> frozenset[str]:
        """Drop the broadcaster-granted capabilities — when they disconnect, or Twitch stops accepting
        their token. What the bot earned by being a moderator is untouched."""
        settings = self.policy.channel_settings(channel_id)
        kept = frozenset(set(settings.capabilities) - FULL_CAPABILITIES) if settings else frozenset({CHAT})
        await self._store(channel_id, kept)
        return kept

    async def _store(self, channel_id: str, capabilities: frozenset[str]) -> None:
        settings = self.policy.channel_settings(channel_id)
        tier = tier_for(capabilities)
        if settings is not None and settings.capabilities == capabilities and settings.tier == tier:
            return

        async def write(repo: PolicyRepository) -> None:
            actor = Actor(None, "system")
            await repo.set_channel_field(channel_id, "capabilities", capabilities, actor)
            await repo.set_channel_field(channel_id, "tier", tier, actor)

        await self.policy.mutate(write)
        log.info("capabilities.changed", channel=channel_id, tier=tier, has=sorted(capabilities))


def missing_for(requires: tuple[str, ...] | list[str], settings: Any) -> list[str]:
    """Which declared requirements this channel doesn't meet — used to explain, not to fail late."""
    have = set(getattr(settings, "capabilities", ()) or ())
    return sorted(set(requires) - have)
