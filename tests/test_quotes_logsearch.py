"""The `quotes` and `logsearch` modules (architecture §12)."""

from __future__ import annotations

import dataclasses
import random
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from doomtp_bot.audit.log import read_audit
from doomtp_bot.filters.service import FilterService
from doomtp_bot.modules import builtin_registry
from doomtp_bot.modules.logsearch import ago
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.quotes import QuoteService
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
        ctx = self.runtime.make_context(
            channel=channel, invoker=chatter, rng=random.Random(seed), clock=lambda: NOW_S
        )
        report = await self.runtime.run(text, ctx)
        assert report is not None
        return report

    async def said(self, message_id: str, login: str, text: str, minutes_ago: int, **flags: Any) -> None:
        at = int((NOW_S - minutes_ago * 60) * 1000)
        columns = "".join(f", {name}" for name in flags)
        await self.dbs.chatlog.execute(
            "INSERT INTO messages (message_id, channel_id, user_id, user_login, display_name, text,"
            f" sent_at, received_at{columns}) VALUES (%s, %s, '1', %s, %s, %s, %s, %s{', %s' * len(flags)})",
            (message_id, CHANNEL_ID, login, login.title(), text, at, at, *flags.values()),
        )


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = await policy_with_channels(
        dbs.bot, (CHANNEL_ID, CHANNEL_LOGIN), joined=True, clock=TickingClock()
    )
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
            "quotes": QuoteService(dbs.bot),
            "chatlog_db": dbs.chatlog,
        },
    )
    yield Harness(dbs, policy, runtime)


# ── quotes ─────────────────────────────────────────────────────────────────
async def test_quotes_are_numbered_read_back_and_searched(h: Harness) -> None:
    assert (await h.run("alice", "!quote")).result.code == Code.NOT_FOUND
    h.live = True
    assert (await h.run("mod", '!quote add I meant   to do "that"')).send == "added #1"
    h.live = False
    assert (await h.run("mod", "!quote add second one, meant too")).send == "added #2"

    first = await h.run("alice", "!quote 1")
    assert first.send is not None and first.send.startswith('#1: I meant   to do "that" [Doom, ')
    second = await h.run("alice", "!quote #2")  # not live when added: the date alone
    assert second.send is not None and re.fullmatch(
        r"#2: second one, meant too \[\d{4}-\d\d-\d\d\]", second.send
    )
    searched = await h.run("alice", "!quote MEANT")
    assert (
        searched.send is not None and searched.send.startswith("#2: ") and searched.send.endswith("(1 of 2)")
    )
    assert (await h.run("alice", "!quote nothing like it")).result.code == Code.NOT_FOUND
    assert (await h.run("alice", "!quote", seed=1)).result.data["number"] in (1, 2)


async def test_only_moderators_change_quotes_and_numbers_are_never_reused(h: Harness) -> None:
    denied = await h.run("alice", "!quote add mine")
    assert denied.result.code == Code.DENIED and denied.send is None
    await h.run("mod", "!quote add one")
    await h.run("mod", "!quote add two")
    assert (await h.run("alice", "!quote del 2")).result.code == Code.DENIED
    assert (await h.run("mod", "!quote del 2")).send == "deleted #2"
    assert (await h.run("mod", "!quote del 2")).result.code == Code.NOT_FOUND
    assert (await h.run("alice", "!quote 2")).result.code == Code.NOT_FOUND
    assert (await h.run("mod", "!quote add three")).send == "added #3"  # #2 stays taken

    actions = [(row["action"], row["target"]) for row in await read_audit(h.dbs.bot, limit=10)]
    assert ("quote.delete", "2") in actions and ("quote.add", "3") in actions


async def test_a_quote_goes_through_the_filter_before_it_is_kept(h: Harness) -> None:
    report = await h.run("mod", "!quote add what a slur")
    assert report.result.code != 0 and "filter" in (report.result.message or "")
    assert await QuoteService(h.dbs.bot).get(CHANNEL_ID, 1) is None


async def test_quotes_are_kept_per_channel(h: Harness) -> None:
    service = QuoteService(h.dbs.bot)
    await service.add("elsewhere", "not here", Actor(None, "test"))
    assert (await h.run("alice", "!quote")).result.code == Code.NOT_FOUND
    assert (await service.add(CHANNEL_ID, "here", Actor(None, "test"))).number == 1


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
    await h.policy.mutate(
        lambda r: r.set_channel_field(CHANNEL_ID, "log_enabled", False, Actor(None, "test"))
    )
    assert (await h.run("mod", "!logsearch anything")).result.message == "this channel's chat isn't logged"


def test_ago_reads_like_chat() -> None:
    assert ago(0, 30_000) == "just now"
    assert ago(0, 90_000) == "1m ago"
    assert ago(0, 2 * 86_400_000) == "2d ago"
