"""The `logsearch` module (architecture §12). Quotes are a derived pack now: tests/test_quotes_pack.py."""

from __future__ import annotations

import dataclasses
import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from doomtp_bot.filters.service import FilterService
from doomtp_bot.modules import builtin_registry
from doomtp_bot.modules.logsearch import ago
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import RunReport, Runtime
from doomtp_bot.runtime.result import Code
from doomtp_bot.storage.db import Databases
from tests.fakes import TickingClock, policy_with_channels

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {"alice": ("400", "alice", "Alice"), "mod": ("300", "mod", "Mod")}
NOW_S = 1_800_000_000.0


@dataclass
class Harness:
    dbs: Databases
    policy: PolicyService
    runtime: Runtime
    live: bool = False

    async def run(self, who: str, text: str, *, seed: int = 0) -> RunReport:
        channel = dataclasses.replace(
            self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN), prefix="!", live=self.live, game="Doom"
        )
        user = USERS[who]
        badges = frozenset({"moderator"}) if who == "mod" else frozenset()
        chatter = self.policy.build_chatter(CHANNEL_ID, user[0], user[1], user[2], badges)
        ctx = self.runtime.make_context(channel=channel, invoker=chatter, rng=random.Random(seed), clock=lambda: NOW_S)
        report = await self.runtime.run(text, ctx)
        assert report is not None
        return report

    async def said(self, message_id: str, login: str, text: str, minutes_ago: int, **flags: Any) -> None:
        at = int((NOW_S - minutes_ago * 60) * 1000)
        columns = "".join(f", {name}" for name in flags)
        await self.dbs.chatlog.execute(
            "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, raw, raw_format,"
            f" sent_at, received_at{columns}) VALUES (%s, %s, '1', %s, %s, %s, 'legacy', %s, %s{', %s' * len(flags)})",
            (
                message_id,
                CHANNEL_ID,
                login,
                text,
                json.dumps({"chatter_user_name": login.title()}),
                at,
                at,
                *flags.values(),
            ),
        )


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = await policy_with_channels(dbs.bot, (CHANNEL_ID, CHANNEL_LOGIN), joined=True, clock=TickingClock())
    filters = FilterService(dbs.bot)
    await filters.reload()
    await filters.add(
        channel_id=CHANNEL_ID, pattern="slur", kind="word", action="block", actor_user_id=None, via="chat"
    )
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        services={
            "policy": policy,
            "filters": filters,
            "chatlog_db": dbs.chatlog,
        },
    )
    yield Harness(dbs, policy, runtime)


# ── logsearch ──────────────────────────────────────────────────────────────
async def test_logsearch_finds_the_newest_visible_message(h: Harness) -> None:
    await h.said("m1", "alice", "the speedrun starts at 8", 180)
    await h.said("m2", "bob", "speedrun hype", 90)
    await h.said("m3", "carol", "speedrun is fake and so are you", 30, deleted_at=1)
    await h.said("m4", "dave", "speedrun spam", 20, cleared_at=1)

    found = await h.run("mod", "!logsearch speedrun")
    assert found.send == "Bob, 1h ago: speedrun hype (1 of 2)"
    assert [m["login"] for m in found.result.data["messages"]] == ["bob", "alice"]
    by_alice = await h.run("mod", "!logsearch @Alice speedrun")
    assert by_alice.send == "Alice, 3h ago: the speedrun starts at 8"
    assert (await h.run("mod", "!logsearch fake")).result.code == Code.NOT_FOUND  # deleted stays gone
    assert (await h.run("alice", "!logsearch speedrun")).result.code == Code.DENIED


async def test_logsearch_says_when_a_channel_is_not_logged(h: Harness) -> None:
    await h.policy.mutate(lambda r: r.set_channel_field(CHANNEL_ID, "log_enabled", False, Actor(None, "test")))
    assert (await h.run("mod", "!logsearch anything")).result.message == "this channel's chat isn't logged"


def test_ago_reads_like_chat() -> None:
    assert ago(0, 30_000) == "just now"
    assert ago(0, 90_000) == "1m ago"
    assert ago(0, 2 * 86_400_000) == "2d ago"
