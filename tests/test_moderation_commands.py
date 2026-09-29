"""`timeout` and `shoutout`, and what the runtime does for commands that act on Twitch (architecture §4.3)."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import doomtp_bot.modules
from doomtp_bot.core.capabilities import BROADCAST, MODERATE, RAIDS
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
    actions: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    refuse: str | None = None

    async def resolve_user(self, login: str) -> dict[str, str] | None:
        if self.during_lookup is not None:
            self.during_lookup()
        found = USERS.get(login.lower())
        return {"id": found[0], "name": found[1], "display": found[2]} if found else None

    async def timeout_user(self, channel_id: str, user_id: str, seconds: int, reason: str) -> bool:
        self.timeouts.append((channel_id, user_id, seconds, reason))
        return True

    async def shoutout(self, channel_id: str, to_user_id: str) -> str | None:
        if self.refuse_shoutout is None:
            self.shoutouts.append((channel_id, to_user_id))
        return self.refuse_shoutout

    async def delete_message(self, channel_id: str, message_id: str) -> bool:
        self.actions.append(("delete_message", (channel_id, message_id)))
        return True

    async def find_game(self, name: str) -> dict[str, str] | None:
        return {"id": "509658", "name": "Just Chatting"} if name.lower() == "just chatting" else None

    def __getattr__(self, method: str) -> Any:
        """Every other action: recorded, and refused with `refuse` when that is set."""

        async def act(*args: Any, **kwargs: Any) -> str | None:
            if self.refuse is None:
                self.actions.append((method, (*args, *kwargs.items())))
            return self.refuse

        return act


@dataclass
class Harness:
    policy: PolicyService
    runtime: Runtime
    twitch: FakeTwitch
    live: bool = False
    removed: bool = False  # the moderation index says the asking message is gone
    reply_to: dict[str, str] | None = None  # the message the asking one replies to

    def context(self, who: str) -> Any:
        channel = dataclasses.replace(
            self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN), prefix="!", live=self.live
        )
        user = USERS[who]
        badges = frozenset({"moderator"}) if who == "mod" else frozenset()
        chatter = self.policy.build_chatter(CHANNEL_ID, user[0], user[1], user[2], badges)
        return self.runtime.make_context(
            channel=channel, invoker=chatter, reply_to=self.reply_to, is_cancelled=lambda: self.removed
        )

    async def run(self, who: str, text: str) -> RunReport:
        report = await self.runtime.run(text, self.context(who))
        assert report is not None
        return report

    async def moderator_here(self, granted: bool, *also: str) -> None:
        capabilities = ({"chat", MODERATE} if granted else {"chat"}) | set(also)
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
async def test_a_shoutout_is_the_card_alone_and_fails_while_offline(h: Harness) -> None:
    offline = await h.run("mod", "!shoutout @friend")
    assert offline.result.code == Code.FAIL and "isn't live" in (offline.result.message or "")
    assert h.twitch.shoutouts == []

    h.live = True
    live = await h.run("mod", "!shoutout alice")
    assert (live.result.code, live.send, live.result.data) == (0, None, {"user": "alice"})
    assert h.twitch.shoutouts == [(CHANNEL_ID, "400")]


async def test_a_refused_card_fails_with_twitchs_reason(h: Harness) -> None:
    h.live = True
    h.twitch.refuse_shoutout = (
        "Twitch allows one shoutout every 2 minutes, and the same streamer once an hour"
    )
    report = await h.run("mod", "!shoutout @friend")
    assert report.result.code == Code.FAIL and "2 minutes" in (report.result.message or "")
    assert (await h.run("mod", "!shoutout @doomtp")).result.message == "that's this channel"


async def test_the_card_waits_on_the_moderation_index_too(h: Harness) -> None:
    h.live = True
    h.twitch.during_lookup = lambda: setattr(h, "removed", True)  # removed while @friend is looked up
    report = await h.run("mod", "!shoutout @friend")
    assert report.result.code == Code.CANCELLED and h.twitch.shoutouts == []


# ── ban, unban, warn ───────────────────────────────────────────────────────
async def test_ban_unban_and_warn_act_as_the_bot_and_spare_the_untouchable(h: Harness) -> None:
    assert (await h.run("mod", "!ban @spammer bot account")).send == "Spammer is banned"
    assert h.twitch.actions[-1] == ("ban_user", (CHANNEL_ID, "500", "mod: bot account"))
    assert (await h.run("mod", "!untimeout spammer")).send == "Spammer can chat again"
    assert h.twitch.actions[-1] == ("unban_user", (CHANNEL_ID, "500"))
    assert (await h.run("mod", "!warn @alice no spoilers")).send == "Alice is warned"
    assert h.twitch.actions[-1] == ("warn_user", (CHANNEL_ID, "400", "no spoilers"))

    done = len(h.twitch.actions)
    assert "the broadcaster" in ((await h.run("mod", "!ban @doomtp")).result.message or "")
    assert "the bot" in ((await h.run("mod", "!warn @doomtp_bot hi")).result.message or "")
    assert (await h.run("mod", "!warn @alice")).result.code == Code.USAGE  # a warning needs its reason
    assert (await h.run("alice", "!ban @spammer")).result.code == Code.DENIED
    assert len(h.twitch.actions) == done


async def test_twitchs_refusal_fails_the_command_with_its_reason(h: Harness) -> None:
    h.twitch.refuse = "Twitch said: The user is already banned"
    report = await h.run("mod", "!ban @spammer")
    assert (report.result.code, report.result.message) == (
        Code.FAIL,
        "Twitch said: The user is already banned",
    )


# ── chat-wide ──────────────────────────────────────────────────────────────
async def test_announce_takes_an_optional_colour_and_is_its_own_output(h: Harness) -> None:
    report = await h.run("mod", "!announce purple giveaway soon")
    assert report.send is None and h.twitch.actions[-1] == (
        "announce",
        (CHANNEL_ID, "giveaway soon", "purple"),
    )
    await h.run("mod", "!announce purple")  # a colour word alone is the text
    assert h.twitch.actions[-1] == ("announce", (CHANNEL_ID, "purple", "primary"))


async def test_chatmode_turns_each_mode_on_and_off(h: Harness) -> None:
    assert (await h.run("mod", "!chatmode slow")).send == "slow mode is on (30s)"
    assert h.twitch.actions[-1] == (
        "update_chat_settings",
        (CHANNEL_ID, {"slow_mode": True, "slow_mode_wait_time": 30}),
    )
    assert (await h.run("mod", "!chatmode followers 1h")).send == "followers-only mode is on (1h)"
    assert h.twitch.actions[-1][1][1] == {"follower_mode": True, "follower_mode_duration": 60}
    assert (await h.run("mod", "!chatmode emoteonly off")).send == "emote-only mode is off"
    assert h.twitch.actions[-1][1][1] == {"emote_mode": False}

    done = len(h.twitch.actions)
    assert "between 3 and 120" in ((await h.run("mod", "!chatmode slow 5m")).result.message or "")
    assert (await h.run("mod", "!chatmode subsonly maybe")).result.code == Code.USAGE
    assert (await h.run("mod", "!chatmode loud")).result.code == Code.USAGE
    assert len(h.twitch.actions) == done


async def test_clear_and_shield(h: Harness) -> None:
    assert (await h.run("mod", "!clear")).send is None
    assert h.twitch.actions[-1] == ("clear_chat", (CHANNEL_ID,))
    assert (await h.run("mod", "!shield on")).send == "Shield Mode is on"
    assert h.twitch.actions[-1] == ("shield_mode", (CHANNEL_ID, True))


# ── acting on the replied-to message ───────────────────────────────────────
async def test_delete_and_pin_act_on_the_message_replied_to(h: Harness) -> None:
    assert "reply to the message" in ((await h.run("mod", "!delete")).result.message or "")
    h.reply_to = {"message_id": "m1", "id": "500", "name": "spammer", "display": "Spammer"}
    await h.run("mod", "!delete")
    assert h.twitch.actions[-1] == ("delete_message", (CHANNEL_ID, "m1"))
    await h.run("mod", "!pin 5m")
    assert h.twitch.actions[-1] == ("pin_message", (CHANNEL_ID, "m1", 300))
    assert "between 30 seconds" in ((await h.run("mod", "!pin 1h")).result.message or "")
    await h.run("mod", "!unpin")
    assert h.twitch.actions[-1] == ("unpin_message", (CHANNEL_ID, "m1"))

    h.reply_to = {"message_id": "m2", "id": BOT_ID, "name": "doomtp_bot", "display": "doomtp_bot"}
    await h.run("mod", "!delete")  # the bot's own messages can go
    assert h.twitch.actions[-1] == ("delete_message", (CHANNEL_ID, "m2"))
    h.reply_to = {"message_id": "m3", "id": CHANNEL_ID, "name": "doomtp", "display": "DoomTP"}
    assert "broadcaster" in ((await h.run("mod", "!delete")).result.message or "")


# ── on the broadcaster's token ─────────────────────────────────────────────
async def test_channel_commands_need_the_broadcasters_grant(h: Harness) -> None:
    unavailable = await h.run("mod", "!settitle new title")
    assert "broadcast" in str(unavailable.result.data) and h.twitch.actions == []

    await h.moderator_here(True, BROADCAST, RAIDS)
    assert (await h.run("mod", "!settitle speedrun, any%")).send == "title set: speedrun, any%"
    assert h.twitch.actions[-1] == ("update_channel", (CHANNEL_ID, ("title", "speedrun, any%")))
    assert (await h.run("mod", "!setgame just chatting")).send == "category set: Just Chatting"
    assert h.twitch.actions[-1] == ("update_channel", (CHANNEL_ID, ("game_id", "509658")))
    assert (await h.run("mod", "!setgame nonsense")).result.code == Code.FAIL

    assert "isn't live" in ((await h.run("mod", "!marker boss down")).result.message or "")
    h.live = True
    assert (await h.run("mod", "!marker boss down")).send == "marker placed"
    assert h.twitch.actions[-1] == ("stream_marker", (CHANNEL_ID, "boss down"))


async def test_a_raid_is_the_broadcasters_alone(h: Harness) -> None:
    await h.moderator_here(True, RAIDS)
    assert (await h.run("mod", "!raid @friend")).result.code == Code.DENIED
    report = await h.run("doomtp", "!raid @friend")
    assert report.send == "raiding Friend, Twitch sends it after the countdown"
    assert h.twitch.actions[-1] == ("start_raid", (CHANNEL_ID, "600"))


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
