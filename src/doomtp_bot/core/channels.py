"""Joined channels: onboarding at the basic tier, subscriptions and log sessions (ADR-0007)."""

from __future__ import annotations

from typing import Protocol

import structlog

from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.policy.snapshot import ChannelSettings

log = structlog.get_logger(__name__)


class Subscriber(Protocol):
    bot_id: str | None

    async def subscribe_channel(self, channel_id: str) -> list[str]: ...


class SessionLog(Protocol):
    async def start_session(self, channel_id: str) -> None: ...

    async def end_session(self, channel_id: str, reason: str) -> None: ...


class ChannelManager:
    def __init__(self, policy: PolicyService, subscriber: Subscriber | None, sessions: SessionLog) -> None:
        self.policy = policy
        self.subscriber = subscriber
        self.sessions = sessions

    def active_channels(self) -> list[ChannelSettings]:
        return [c for c in self.policy.snapshot.channels.values() if c.active and c.status == "joined"]

    def is_active(self, channel_id: str) -> bool:
        settings = self.policy.channel_settings(channel_id)
        return settings is not None and settings.active and settings.status == "joined"

    async def join(self, channel_id: str, login: str, actor: Actor) -> list[str]:
        """Mark the channel joined and subscribe. Returns failed subscription types (empty on success)."""
        await self.policy.mutate(lambda repo: repo.ensure_channel(channel_id, login, actor))
        settings = self.policy.channel_settings(channel_id)
        if settings is not None and (settings.status != "joined" or not settings.active):
            await self.policy.mutate(
                lambda repo: repo.set_channel_field(channel_id, "status", "joined", actor)
            )
            await self.policy.mutate(lambda repo: repo.set_channel_field(channel_id, "active", 1, actor))
        failed = await self._subscribe(channel_id)
        log.info("channel.join", channel=login, failed=failed)
        return failed

    async def part(self, channel_id: str, actor: Actor) -> None:
        await self.policy.mutate(lambda repo: repo.set_channel_field(channel_id, "status", "parted", actor))
        await self.policy.mutate(lambda repo: repo.set_channel_field(channel_id, "active", 0, actor))
        await self.sessions.end_session(channel_id, "part")
        log.info("channel.part", channel_id=channel_id)

    async def ensure_home(self, bot_id: str, bot_login: str) -> None:
        """The bot's own channel is always joined, so broadcasters can type !join there."""
        if not self.is_active(bot_id):
            await self.join(bot_id, bot_login, Actor(None, "system"))

    async def subscribe_all(self) -> None:
        """Subscribe every joined channel that isn't subscribed yet on this connection."""
        is_subscribed = getattr(self.subscriber, "is_subscribed", None)
        for settings in self.active_channels():
            if is_subscribed is not None and is_subscribed(settings.channel_id):
                continue
            await self._subscribe(settings.channel_id)

    async def _subscribe(self, channel_id: str) -> list[str]:
        if self.subscriber is None:
            return ["not_connected"]
        failed = await self.subscriber.subscribe_channel(channel_id)
        if not failed:
            await self.sessions.start_session(channel_id)
        return failed
