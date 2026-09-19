"""Joined channels: onboarding at the basic tier, subscriptions and log sessions (ADR-0007)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

import structlog

from doomtp_bot.lang.parser import DEFAULT_PREFIX
from doomtp_bot.policy.repository import Actor, PolicyRepository
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.policy.snapshot import ChannelSettings

log = structlog.get_logger(__name__)


class Subscriber(Protocol):
    bot_id: str | None

    async def subscribe_channel(self, channel_id: str) -> list[str]:
        """Idempotent per connection. Returns the subscription types that failed."""
        ...

    async def unsubscribe_channel(self, channel_id: str) -> None: ...


class SessionLog(Protocol):
    async def start_session(self, channel_id: str) -> None: ...

    async def end_session(self, channel_id: str, reason: str) -> None: ...


def _is_joined(settings: ChannelSettings | None) -> bool:
    return settings is not None and settings.active and settings.status == "joined"


class ChannelManager:
    def __init__(
        self,
        policy: PolicyService,
        subscriber: Subscriber | None,
        sessions: SessionLog,
        *,
        default_prefix: str = DEFAULT_PREFIX,
        on_joined: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self.policy = policy
        self.subscriber = subscriber
        self.sessions = sessions
        self.default_prefix = default_prefix
        self.on_joined = on_joined  # the capability probe, once the channel is subscribed (ADR-0007)

    def active_channels(self) -> list[ChannelSettings]:
        return [c for c in self.policy.snapshot.channels.values() if _is_joined(c)]

    def is_active(self, channel_id: str) -> bool:
        return _is_joined(self.policy.channel_settings(channel_id))

    async def join(self, channel_id: str, login: str, actor: Actor) -> list[str]:
        """Mark the channel joined and subscribe. Returns failed subscription types (empty on success)."""
        await self._mark_joined(channel_id, login, actor)
        failed = await self._subscribe(channel_id)
        log.info("channel.join", channel=login, failed=failed)
        return failed

    async def part(self, channel_id: str, actor: Actor) -> None:
        async def mark_parted(repo: PolicyRepository) -> None:
            await repo.set_channel_field(channel_id, "status", "parted", actor)
            await repo.set_channel_field(channel_id, "active", 0, actor)

        await self.policy.mutate(mark_parted)
        if self.subscriber is not None:
            await self.subscriber.unsubscribe_channel(channel_id)
        await self.sessions.end_session(channel_id, "part")
        log.info("channel.part", channel_id=channel_id)

    async def ensure_home(self, bot_id: str, bot_login: str) -> None:
        """The bot's own channel is always joined, so broadcasters can type !join there. subscribe_all() connects it."""
        if not self.is_active(bot_id):
            await self._mark_joined(bot_id, bot_login, Actor(None, "system"))

    async def subscribe_all(self) -> None:
        """Subscribe every joined channel (at startup and after re-authorization)."""
        for settings in self.active_channels():
            await self._subscribe(settings.channel_id)

    async def _mark_joined(self, channel_id: str, login: str, actor: Actor) -> None:
        async def mark(repo: PolicyRepository) -> None:
            await repo.ensure_channel(channel_id, login, actor, self.default_prefix)
            await repo.set_channel_field(channel_id, "status", "joined", actor)
            await repo.set_channel_field(channel_id, "active", 1, actor)

        if not self.is_active(channel_id):
            await self.policy.mutate(mark)

    async def _subscribe(self, channel_id: str) -> list[str]:
        if self.subscriber is None:
            return ["not_connected"]
        failed = await self.subscriber.subscribe_channel(channel_id)
        if not failed:
            await self.sessions.start_session(channel_id)
            if self.on_joined is not None:
                try:
                    await self.on_joined(channel_id)
                except Exception:  # probing is best-effort; a joined channel still works
                    log.exception("channel.on_joined_failed", channel_id=channel_id)
        return failed
