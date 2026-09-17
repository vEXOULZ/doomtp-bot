"""Permissions, cooldowns, toggles, callbacks and chat admin commands against a real bot.db (ADR-0006)."""

from __future__ import annotations

import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.context import Chatter
from doomtp_bot.runtime.engine import RunReport, Runtime
from doomtp_bot.runtime.registry import CommandRegistry, command
from doomtp_bot.runtime.result import Code, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown
from doomtp_bot.storage.db import Databases

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
OWNER_ID = "1"
USERS = {
    "streamer": {"id": CHANNEL_ID, "name": "doomtp", "display": "DoomTP"},
    "mod": {"id": "200", "name": "mod", "display": "Mod"},
    "vip": {"id": "300", "name": "vip", "display": "Vip"},
    "viewer": {"id": "400", "name": "viewer", "display": "Viewer"},
    "viewer2": {"id": "401", "name": "viewer2", "display": "Viewer2"},
    "owner": {"id": OWNER_ID, "name": "owner", "display": "Owner"},
}
BADGES = {"streamer": {"broadcaster"}, "mod": {"moderator"}, "vip": {"vip"}}


@dataclass
class FakeClock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@command(
    CommandSpec(
        name="dice",
        module="games",
        summary="test command with cooldowns",
        default_cooldowns={"everyone": Cooldown(tier_s=10, user_s=30)},
    )
)
async def dice(ctx: Any, args: Any, stdin: Result | None) -> Result:
    return Result.success("rolled")


@command(CommandSpec(name="caps", module="games", summary="needs a capability", requires=("redemptions",)))
async def caps(ctx: Any, args: Any, stdin: Result | None) -> Result:
    return Result.success("ok")


@dataclass
class Harness:
    dbs: Databases
    policy: PolicyService
    runtime: Runtime
    clock: FakeClock
    sent: list[str] = field(default_factory=list)

    def chatter(self, who: str) -> Chatter:
        u = USERS[who]
        return self.policy.build_chatter(
            CHANNEL_ID, u["id"], u["name"], u["display"], frozenset(BADGES.get(who, set()))
        )

    async def say(self, who: str, text: str, **ctx_kwargs: Any) -> RunReport | None:
        channel = self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN, **ctx_kwargs.pop("live", {}))
        ctx = self.runtime.make_context(
            channel=channel, invoker=self.chatter(who), rng=random.Random(1), **ctx_kwargs
        )
        return await self.runtime.run(text, ctx)

    async def reply(self, who: str, text: str, **kw: Any) -> str | None:
        report = await self.say(who, text, **kw)
        assert report is not None
        return report.send

    async def audit_actions(self) -> list[str]:
        async with self.dbs.bot.execute("SELECT action FROM audit_log ORDER BY id") as cur:
            return [r[0] for r in await cur.fetchall()]


async def resolve_user(login: str) -> dict[str, Any] | None:
    return next((dict(u) for u in USERS.values() if u["name"] == login), None)


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    clock = FakeClock()
    policy = PolicyService(dbs.bot, bot_owner_ids=frozenset({OWNER_ID}), clock=clock)
    await policy.reload()
    registry: CommandRegistry = builtin_registry()
    registry.extend((dice, caps))
    runtime = Runtime(
        registry, policy=policy, callbacks=policy, resolve_user=resolve_user, services={"policy": policy}
    )
    yield Harness(dbs, policy, runtime, clock)


# ── roles and ranks ────────────────────────────────────────────────────────
async def test_ranks_from_badges_broadcaster_and_owner(h: Harness) -> None:
    assert h.chatter("viewer").rank == 0
    assert h.chatter("vip").rank == 60
    assert h.chatter("mod").rank == 80
    assert h.chatter("streamer").rank == 100  # user_id == channel_id, even without the badge
    assert h.chatter("owner").rank == 10000
    assert h.chatter("mod").roles[0] == "moderator"


async def test_custom_role_membership_and_expiry(h: Harness) -> None:
    actor = Actor(CHANNEL_ID)
    await h.policy.mutate(lambda r: r.ensure_channel(CHANNEL_ID, CHANNEL_LOGIN, actor))
    role_id = await h.policy.mutate(lambda r: r.create_role(CHANNEL_ID, "ambassador", 50, actor))
    await h.policy.mutate(
        lambda r: r.add_member(role_id, CHANNEL_ID, "ambassador", "400", "viewer", None, actor)
    )  # type: ignore[arg-type]
    assert h.chatter("viewer").rank == 50 and "ambassador" in h.chatter("viewer").roles
    expired = int(time.time() * 1000) - 1
    await h.policy.mutate(
        lambda r: r.add_member(role_id, CHANNEL_ID, "ambassador", "400", "viewer", expired, actor)
    )  # type: ignore[arg-type]
    assert h.chatter("viewer").rank == 0


# ── permissions ────────────────────────────────────────────────────────────
async def test_admin_commands_denied_silently_for_viewers(h: Harness) -> None:
    report = await h.say("viewer", "!role create x 10")
    assert report is not None and (report.result.code, report.send) == (Code.DENIED, None)


async def test_perm_set_and_allowed_roles(h: Harness) -> None:
    assert await h.reply("viewer", "!dice") == "rolled"
    assert await h.reply("mod", "!perm set dice vip") == "dice now requires vip"
    h.clock.now += 100
    assert await h.reply("viewer", "!dice") is None
    assert await h.reply("vip", "!dice") == "rolled"
    assert await h.reply("streamer", "!role create ambassador 50") == "created role ambassador (rank 50)"
    assert await h.reply("streamer", "!role add ambassador @viewer") == "gave ambassador to Viewer"
    assert await h.reply("mod", "!perm allow dice ambassador") == "dice is also allowed for: ambassador"
    h.clock.now += 100
    assert await h.reply("viewer", "!dice") == "rolled"
    assert await h.reply("mod", "!perm show dice") == "dice: requires vip, or exactly: ambassador"


async def test_unknown_required_role_fails_closed(h: Harness) -> None:
    actor = Actor(OWNER_ID)
    await h.policy.mutate(lambda r: r.ensure_channel(CHANNEL_ID, CHANNEL_LOGIN, actor))
    await h.policy.mutate(lambda r: r.set_command_rule(CHANNEL_ID, "ping", "ghost_role", None, actor))
    report = await h.say("owner", "!ping")
    assert report is not None and report.result.code == Code.DENIED


async def test_mods_cannot_change_admin_command_permissions(h: Harness) -> None:
    assert (
        await h.reply("mod", "!perm set role everyone")
        == "only bot admins can change admin command permissions"
    )


async def test_role_grant_rules(h: Harness) -> None:
    assert await h.reply("mod", "!role create helper 90") == "you can only create roles ranked below your own"
    assert await h.reply("mod", "!role create helper 70") == "created role helper (rank 70)"
    assert await h.reply("streamer", "!role create lead 95") == "created role lead (rank 95)"
    assert await h.reply("mod", "!role add lead @viewer") == "you can't manage lead"
    assert await h.reply("streamer", "!role add lead @mod 1h") == "gave lead to Mod for 1h"
    assert await h.reply("streamer", "!role who lead") == "lead: mod"
    assert await h.reply("mod", "!role add moderator @viewer") == "you can't manage moderator"


async def test_bot_owner_manages_admins(h: Harness) -> None:
    report = await h.say("streamer", "!admin add @viewer")
    assert report is not None and report.result.code == Code.DENIED
    assert await h.reply("owner", "!admin add @viewer") == "Viewer is now a bot admin"
    assert h.chatter("viewer").rank == 1000


# ── toggles and capabilities ───────────────────────────────────────────────
async def test_module_and_command_toggles(h: Harness) -> None:
    assert await h.reply("mod", "!module disable games") == "games disabled here"
    report = await h.say("viewer", "!dice")
    assert report is not None and (report.result.code, report.send) == (Code.UNKNOWN, None)
    assert await h.reply("mod", "!cmd enable dice") == "dice enabled"
    assert await h.reply("viewer", "!dice") == "rolled"
    assert await h.reply("mod", "!module disable core") == "core can't be turned off"
    assert (
        await h.reply("mod", "!module disable games global") == "only bot admins can change global settings"
    )
    assert await h.reply("owner", "!module disable games global") == "games disabled everywhere"
    h.clock.now += 100
    report = await h.say("viewer", "!dice")
    assert (
        report is not None and report.result.code == Code.UNKNOWN
    )  # global kill switch beats channel enable


async def test_help_lists_only_runnable_commands(h: Harness) -> None:
    viewer_help = await h.reply("viewer", "!help")
    assert (
        viewer_help is not None
        and "ping" in viewer_help
        and "role" not in viewer_help
        and "true" not in viewer_help
    )
    h.clock.now += 100
    mod_help = await h.reply("mod", "!help")
    assert mod_help is not None and "role" in mod_help and "admin" not in mod_help
    h.clock.now += 100
    assert await h.reply("viewer", "!help role") == "no command named role"


async def test_capabilities_required(h: Harness) -> None:
    report = await h.say("viewer", "!caps")
    assert report is not None and report.result.code == Code.UNKNOWN
    report = await h.say("viewer", "!caps", live={"capabilities": frozenset({"redemptions"})})
    assert report is not None and report.send == "ok"


# ── cooldowns ──────────────────────────────────────────────────────────────
async def test_tier_and_user_buckets_both_required(h: Harness) -> None:
    assert await h.reply("viewer", "!dice") == "rolled"
    report = await h.say("viewer2", "!dice")  # shared everyone bucket (10s)
    assert report is not None and report.result.code == Code.COOLDOWN
    h.clock.now += 11
    assert await h.reply("viewer2", "!dice") == "rolled"
    h.clock.now += 11
    report = await h.say("viewer", "!dice")  # tier clear, but personal 30s bucket still running
    assert report is not None and report.result.code == Code.COOLDOWN
    assert report.decision is not None and report.decision.info["user_remaining"] == 8


async def test_moderators_default_to_no_cooldown_and_tiers_are_separate(h: Harness) -> None:
    assert await h.reply("viewer", "!dice") == "rolled"
    assert await h.reply("mod", "!dice") == "rolled"
    assert await h.reply("mod", "!dice") == "rolled"
    assert await h.reply("streamer", "!dice") == "rolled"  # rank 100 falls to the moderator 0/0 rule
    assert await h.reply("mod", "!cooldown set dice vip 20 0") == "dice for vip: 20s shared, 0s personal"
    assert await h.reply("vip", "!dice") == "rolled"  # vip tier bucket is separate from everyone's
    report = await h.say("vip", "!dice")
    assert report is not None and report.result.code == Code.COOLDOWN


async def test_cooldown_only_committed_for_executed_commands(h: Harness) -> None:
    assert await h.reply("viewer", "!ping || !dice") == "pong"
    assert await h.reply("viewer2", "!dice") == "rolled"


# ── callbacks ──────────────────────────────────────────────────────────────
async def test_cooldown_callback_is_rendered_and_rate_limited(h: Harness) -> None:
    set_reply = await h.reply(
        "mod", "!callback set on_cooldown command:dice echo {chatter.name}, wait {cooldown.user_remaining}s"
    )
    assert set_reply == "set on_cooldown for command:dice"
    assert await h.reply("viewer", "!dice") == "rolled"
    assert await h.reply("viewer", "!dice") == "viewer, wait 30s"
    assert await h.reply("viewer", "!dice") is None  # one notice per 30s


async def test_denied_callback_from_channel_scope(h: Harness) -> None:
    assert await h.reply(
        "mod", "!callback set on_denied channel echo sorry, {denied.command} needs {denied.required_role}"
    ) == ("set on_denied for channel")
    assert await h.reply("viewer", "!role list") == "sorry, role needs moderator"


async def test_invalid_callback_expression_rejected(h: Harness) -> None:
    reply = await h.reply("mod", "!callback set on_denied channel echo {nope}")
    assert reply is not None and "E_BAD_PLACEHOLDER" in reply


# ── channel settings and audit ─────────────────────────────────────────────
async def test_prefix_change_and_validation(h: Harness) -> None:
    assert await h.reply("mod", "!prefix /x") == "prefix can't start with / or ."
    assert await h.reply("mod", "!prefix ?a") == "prefix can't end with a letter, digit, _, @ or -"
    assert await h.reply("mod", "!prefix ~") == "prefix is now ~"
    assert await h.say("viewer", "!ping") is None
    assert await h.reply("viewer", "~ping") == "pong"


async def test_every_change_is_audited(h: Harness) -> None:
    await h.reply("streamer", "!role create ambassador 50")
    await h.reply("mod", "!cooldown set dice everyone 1 1")
    await h.reply("mod", "!cmd log dice all")
    await h.reply("mod", "!ignore add @viewer2")
    actions = await h.audit_actions()
    assert actions == ["channel.join", "role.create", "command.cooldown", "command.toggle", "ignore.add"]
    assert h.policy.is_ignored(CHANNEL_ID, "401")


async def test_sentinels_cannot_be_restricted_or_cooled_down(h: Harness) -> None:
    """Spec §8: sentinels are always allowed for everyone, with no cooldowns."""
    assert await h.reply("mod", "!perm set true moderator") == "true is always allowed for everyone"
    h.clock.now += 100
    assert await h.reply("mod", "!cooldown set true everyone 60 60") == "true never has cooldowns"
    h.clock.now += 100
    assert await h.reply("mod", "!module disable core") == "core can't be turned off"
    h.clock.now += 100
    # Even a rule written straight to the database doesn't apply to a sentinel.
    await h.policy.mutate(
        lambda repo: repo.set_cooldown(CHANNEL_ID, "true", "everyone", 600, 600, Actor("2", "chat"))
    )
    for _ in range(2):
        assert await h.reply("viewer", "!false || true") is None  # allowed, silent, never on cooldown
