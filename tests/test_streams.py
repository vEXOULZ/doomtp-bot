"""Stream status by Helix polling, and the capability probe (ADR-0007, architecture §10)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from doomtp_bot.core.capabilities import CapabilityProbe, granted_by, tier_for
from doomtp_bot.core.channels import ChannelManager
from doomtp_bot.core.events import StreamStatusChanged
from doomtp_bot.core.streams import StreamPoller, StreamStatus
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.storage.db import Databases
from tests.fakes import policy_with_channels

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
OTHER_ID, OTHER_LOGIN = "200", "friend"
LIVE = {"title": "modding the bot", "game": "Science", "viewers": 12, "started_at": "2026-09-19T10:00:00Z"}


@dataclass
class FakeHelix:
    """Stands in for TwitchService: who is live, and whether a moderator-only subscription is allowed."""

    live: dict[str, dict[str, Any]] = field(default_factory=dict)
    moderator_in: set[str] = field(default_factory=set)
    fail: Exception | None = None
    calls: int = 0

    async def fetch_live(self, channel_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return {k: v for k, v in self.live.items() if k in channel_ids}

    async def try_moderator_subscription(self, channel_id: str) -> bool:
        return channel_id in self.moderator_in


@dataclass
class FakeSessions:
    async def start_session(self, channel_id: str) -> None: ...

    async def end_session(self, channel_id: str, reason: str) -> None: ...


@dataclass
class FakeSubscriber:
    bot_id: str | None = "1"
    failing: set[str] = field(default_factory=set)

    async def subscribe_channel(self, channel_id: str) -> list[str]:
        return ["chat.message"] if channel_id in self.failing else []

    async def unsubscribe_channel(self, channel_id: str) -> None: ...


@dataclass
class Harness:
    policy: PolicyService
    channels: ChannelManager
    helix: FakeHelix
    status: StreamStatus
    poller: StreamPoller
    seen: list[StreamStatusChanged]


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = await policy_with_channels(dbs.bot, (CHANNEL_ID, CHANNEL_LOGIN), (OTHER_ID, OTHER_LOGIN))
    channels = ChannelManager(policy, FakeSubscriber(), FakeSessions())
    helix, status, seen = FakeHelix(), StreamStatus(), []

    async def sink(event: StreamStatusChanged) -> None:
        seen.append(event)

    poller = StreamPoller(source=helix, channels=channels, status=status, sink=sink)
    yield Harness(policy, channels, helix, status, poller, seen)


# ── the poller ─────────────────────────────────────────────────────────────
async def test_going_live_and_offline_is_reported_once(h: Harness) -> None:
    h.helix.live = {CHANNEL_ID: LIVE}
    assert [(c.channel_id, c.live) for c in await h.poller.poll()] == [(CHANNEL_ID, True)]
    assert h.status.is_live(CHANNEL_ID) and not h.status.is_live(OTHER_ID)
    assert h.status.info(CHANNEL_ID)["game"] == "Science"

    assert await h.poller.poll() == []  # still live: nothing to report

    h.helix.live = {}
    assert [(c.channel_id, c.live) for c in await h.poller.poll()] == [(CHANNEL_ID, False)]
    assert not h.status.is_live(CHANNEL_ID)
    assert [(c.channel_id, c.live) for c in h.seen] == [(CHANNEL_ID, True), (CHANNEL_ID, False)]


async def test_a_failed_request_never_declares_everybody_offline(h: Harness) -> None:
    h.helix.live = {CHANNEL_ID: LIVE}
    await h.poller.poll()

    h.helix.fail = RuntimeError("helix is down")
    assert await h.poller.poll() == []
    assert h.status.is_live(CHANNEL_ID)  # the last answer stands
    assert h.poller.last_error is not None

    h.helix.fail = None
    assert await h.poller.poll() == []  # recovering doesn't re-announce what we already knew


async def test_parting_a_channel_is_not_an_offline_event(h: Harness) -> None:
    h.helix.live = {CHANNEL_ID: LIVE}
    await h.poller.poll()
    await h.channels.part(CHANNEL_ID, Actor(None, "test"))

    assert await h.poller.poll() == []
    assert not h.status.is_live(CHANNEL_ID)


# ── the capability probe ───────────────────────────────────────────────────
def test_tier_follows_the_capabilities() -> None:
    assert tier_for(frozenset({"chat"})) == "basic"
    assert tier_for(frozenset({"chat", "moderate"})) == "moderator"
    assert tier_for(frozenset({"chat", "moderate", "redemptions"})) == "full"


async def test_the_probe_finds_and_stores_the_moderator_tier(h: Harness) -> None:
    h.helix.moderator_in = {CHANNEL_ID}
    probe = CapabilityProbe(policy=h.policy, channels=h.channels, prober=h.helix)

    found = await probe.probe_all()
    assert found[CHANNEL_ID] == frozenset({"chat", "moderate", "followers"})
    assert found[OTHER_ID] == frozenset({"chat"})

    modded = h.policy.channel_settings(CHANNEL_ID)
    assert modded is not None and modded.tier == "moderator"
    assert h.policy.channel_settings(OTHER_ID).tier == "basic"  # type: ignore[union-attr]


async def test_the_probe_leaves_what_the_broadcaster_granted_alone(h: Harness) -> None:
    await h.policy.mutate(
        lambda repo: repo.set_channel_field(
            CHANNEL_ID, "capabilities", {"chat", "redemptions"}, Actor(None, "x")
        )
    )
    probe = CapabilityProbe(policy=h.policy, channels=h.channels, prober=h.helix)

    assert "redemptions" in await probe.probe(CHANNEL_ID)
    assert h.policy.channel_settings(CHANNEL_ID).tier == "full"  # type: ignore[union-attr]


async def test_a_broadcaster_grant_adds_to_what_the_probe_found(h: Harness) -> None:
    """The connect flow (ADR-0007 item 5): granted scopes become capabilities the probe won't undo."""
    h.helix.moderator_in = {CHANNEL_ID}
    probe = CapabilityProbe(policy=h.policy, channels=h.channels, prober=h.helix)
    await probe.probe(CHANNEL_ID)

    granted = await probe.grant(CHANNEL_ID, granted_by(["channel:read:redemptions", "bits:read"]))
    assert granted == frozenset({"chat", "moderate", "followers", "redemptions", "bits"})
    assert h.policy.channel_settings(CHANNEL_ID).tier == "full"  # type: ignore[union-attr]
    assert await probe.probe(CHANNEL_ID) == granted  # the hourly probe leaves the grant alone

    kept = await probe.revoke_full(CHANNEL_ID)
    assert kept == frozenset({"chat", "moderate", "followers"})  # being a mod isn't theirs to take back
    assert h.policy.channel_settings(CHANNEL_ID).tier == "moderator"  # type: ignore[union-attr]


async def test_joining_a_channel_probes_it(h: Harness) -> None:
    probe = CapabilityProbe(policy=h.policy, channels=h.channels, prober=h.helix)
    h.channels.on_joined = probe.probe
    h.helix.moderator_in = {"900"}

    await h.channels.join("900", "newcomer", Actor(None, "test"))
    assert h.policy.channel_settings("900").tier == "moderator"  # type: ignore[union-attr]
