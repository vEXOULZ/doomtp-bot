"""`timeout` and `shoutout`, and what the runtime does for commands that act on Twitch (architecture §4.3)."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import doomtp_bot.modules
from doomtp_bot.core.capabilities import MODERATE
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.context import Args, ChannelInfo, CommandContext
from doomtp_bot.runtime.engine import RunReport, Runtime
from doomtp_bot.runtime.explain import explain
from doomtp_bot.runtime.registry import command
from doomtp_bot.runtime.result import Code, Result
from doomtp_bot.runtime.spec import CommandSpec, Param
from doomtp_bot.runtime.variables import ANY, InMemoryVariableStore, WriteOp, key_for
from doomtp_bot.storage.db import Databases
from tests.fakes import policy_with_channels

BOT_ID = "999"
CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {
    "alice": ("400", "alice", "Alice"),
    "mod": ("300", "mod", "Mod"),
    "spammer": ("500", "spammer", "Spammer"),
    "friend": ("600", "friend", "Friend"),
    "owner": ("1", "owner", "Owner"),
    "doomtp": (CHANNEL_ID, "doomtp", "DoomTP"),
    "doomtp_bot": (BOT_ID, "doomtp_bot", "doomtp_bot"),
}


@dataclass
class FakeTwitch:
    bot_id: str = BOT_ID
    timeouts: list[tuple[str, str, int, str]] = field(default_factory=list)
    shoutouts: list[tuple[str, str]] = field(default_factory=list)
    refuse_shoutout: str | None = None
    during_lookup: Any = None  # called while a user is looked up: a moderator acting meanwhile

    async def resolve_user(self, login: str) -> dict[str, str] | None:
        if self.during_lookup is not None:
            self.during_lookup()
        found = USERS.get(login.lower())
        return {"id": found[0], "name": found[1], "display": found[2]} if found else None

    async def timeout_user(self, channel_id: str, user_id: str, seconds: int, reason: str) -> bool:
        self.timeouts.append((channel_id, user_id, seconds, reason))
        return True

    async def last_game(self, user_id: str) -> str | None:
        if self.during_lookup is not None:
            self.during_lookup()
        return "Doom" if user_id == "600" else None

    async def shoutout(self, channel_id: str, to_user_id: str) -> str | None:
        if self.refuse_shoutout is None:
            self.shoutouts.append((channel_id, to_user_id))
        return self.refuse_shoutout


@dataclass
class Harness:
    policy: PolicyService
    runtime: Runtime
    twitch: FakeTwitch
    live: bool = False
    removed: bool = False  # the moderation index says the asking message is gone

    def context(self, who: str) -> Any:
        channel = dataclasses.replace(
            self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN), prefix="!", live=self.live
        )
        user = USERS[who]
        badges = frozenset({"moderator"}) if who == "mod" else frozenset()
        chatter = self.policy.build_chatter(CHANNEL_ID, user[0], user[1], user[2], badges)
        return self.runtime.make_context(channel=channel, invoker=chatter, is_cancelled=lambda: self.removed)

    async def run(self, who: str, text: str) -> RunReport:
        report = await self.runtime.run(text, self.context(who))
        assert report is not None
        return report

    async def moderator_here(self, granted: bool) -> None:
        capabilities = {"chat", MODERATE} if granted else {"chat"}
        await self.policy.mutate(
            lambda r: r.set_channel_field(CHANNEL_ID, "capabilities", capabilities, Actor(None, "test"))
        )


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = await policy_with_channels(
        dbs.bot, (CHANNEL_ID, CHANNEL_LOGIN), joined=True, bot_owner_ids=frozenset({"1"})
    )
    twitch = FakeTwitch()
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        resolve_user=twitch.resolve_user,
        services={"policy": policy, "twitch": twitch},
    )
    harness = Harness(policy, runtime, twitch)
    await harness.moderator_here(True)
    yield harness


# ── timeout ────────────────────────────────────────────────────────────────
async def test_a_moderator_times_a_chatter_out_as_the_bot(h: Harness) -> None:
    report = await h.run("mod", "!timeout @spammer 1h links again")
    assert report.send == "Spammer is timed out for 1h"
    assert h.twitch.timeouts == [(CHANNEL_ID, "500", 3600, "mod: links again")]

    await h.run("mod", "!timeout spammer")
    assert h.twitch.timeouts[-1] == (CHANNEL_ID, "500", 600, "mod")


async def test_timeout_is_for_moderators_and_needs_the_bot_to_be_one(h: Harness) -> None:
    denied = await h.run("alice", "!timeout @spammer")
    assert denied.result.code == Code.DENIED and h.twitch.timeouts == []

    await h.moderator_here(False)
    unavailable = await h.run("mod", "!timeout @spammer")
    assert unavailable.result.code != 0 and "moderate" in str(unavailable.result.data)
    assert h.twitch.timeouts == []


async def test_timeout_leaves_the_broadcaster_the_bot_and_higher_ranks_alone(h: Harness) -> None:
    for target, why in (
        ("doomtp", "the broadcaster"),
        ("doomtp_bot", "the bot"),
        ("owner", "rank at or above you"),
    ):
        report = await h.run("mod", f"!timeout @{target}")
        assert why in (report.result.message or ""), target
    too_long = await h.run("mod", "!timeout @spammer 400h")
    assert too_long.result.code == Code.USAGE and "2 weeks" in (too_long.result.message or "")
    assert h.twitch.timeouts == []


async def test_a_removed_message_stops_the_timeout_right_before_twitch_is_asked(h: Harness) -> None:
    # A moderator deletes the asking message while its target is being looked up: the arguments are
    # already expanded, and the runtime's own look before the command runs catches it.
    h.twitch.during_lookup = lambda: setattr(h, "removed", True)
    report = await h.run("mod", "!timeout @spammer")
    assert report.result.code == Code.CANCELLED and h.twitch.timeouts == []


async def test_explain_run_never_acts_on_twitch(h: Harness) -> None:
    report = await explain(h.runtime, "!timeout @spammer", h.context("mod"), run=True)
    assert report.ran and report.run_result is not None and report.run_result.code == 0
    assert report.would_send is None and h.twitch.timeouts == []


# ── shoutout ───────────────────────────────────────────────────────────────
async def test_a_shoutout_says_where_to_look_and_sends_the_card_only_when_live(h: Harness) -> None:
    offline = await h.run("mod", "!shoutout @friend")
    assert offline.send == "Go check out Friend at twitch.tv/friend — last seen playing Doom"
    assert offline.result.data["card"] is False and h.twitch.shoutouts == []

    h.live = True
    live = await h.run("mod", "!shoutout alice")
    assert live.send == "Go check out Alice at twitch.tv/alice"
    assert live.result.data["card"] is True and h.twitch.shoutouts == [(CHANNEL_ID, "400")]


async def test_a_refused_card_still_says_the_line_and_why(h: Harness) -> None:
    h.live = True
    h.twitch.refuse_shoutout = (
        "Twitch allows one shoutout every 2 minutes, and the same streamer once an hour"
    )
    report = await h.run("mod", "!shoutout @friend")
    assert report.send is not None and report.send.startswith("Go check out Friend")
    assert report.result.data["card"] is False and "2 minutes" in report.result.data["card_skipped"]
    assert (await h.run("mod", "!shoutout @doomtp")).result.message == "that's this channel"


async def test_the_card_waits_on_the_moderation_index_too(h: Harness) -> None:
    h.live = True
    looked_up = {"n": 0}

    def removed_during_game_lookup() -> None:
        looked_up["n"] += 1
        if looked_up["n"] > 1:  # the first call is the argument's user lookup, the second the game
            h.removed = True

    h.twitch.during_lookup = removed_during_game_lookup
    report = await h.run("mod", "!shoutout @friend")
    assert report.result.code == Code.CANCELLED and h.twitch.shoutouts == []


# ── declared reads and writes (architecture §4.2) ──────────────────────────
@command(
    CommandSpec(
        name="deathcount",
        module="test",
        summary="counts deaths",
        params=(Param("1", "name"),),
        writes=("channel.deaths",),
    )
)
async def deathcount(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    target = args.values[0] if args.values else "deaths"
    value = await ctx.variables.buffer(WriteOp("incr", key_for(ctx.exec, "channel", target), 1))
    return Result.success(str(value))


async def test_a_built_in_touches_only_the_variables_its_spec_declares() -> None:
    registry = builtin_registry()
    registry.extend((deathcount,))
    runtime = Runtime(registry, store=InMemoryVariableStore())
    ctx = runtime.make_context(channel=ChannelInfo(id="c1", login="doomtp", prefix="!"), invoker=None)
    ok = await runtime.run("!deathcount", ctx)
    assert ok is not None and ok.send == "1"

    ctx = runtime.make_context(channel=ChannelInfo(id="c1", login="doomtp", prefix="!"), invoker=None)
    stray = await runtime.run("!deathcount wins", ctx)
    assert stray is not None and stray.result.code == Code.DENIED
    assert stray.result.message == "deathcount may not write channel.wins: its spec doesn't declare it"


def test_only_var_declares_every_variable() -> None:
    """`!var` is the documented exception (variable-access-matrix.md §2); nothing else may be."""
    wildcard = [c.spec.name for c in builtin_registry().all() if ANY in c.spec.reads or ANY in c.spec.writes]
    assert wildcard == ["var"]


def test_built_ins_reach_variables_only_through_their_declarations() -> None:
    """`ctx.exec.variables` would get round the declarations; only `!var` reads the access policy there."""
    modules = Path(doomtp_bot.modules.__file__).parent
    offenders = [
        path.name
        for path in modules.glob("*.py")
        if "exec.variables" in path.read_text(encoding="utf-8") and path.name != "variables.py"
    ]
    assert offenders == []
