"""Variables: SQLite store, access matrix, grants, !var and store operators end-to-end (ADR-0010)."""

from __future__ import annotations

import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from doomtp_bot.lang.parser import Context
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.context import Chatter, ExecContext, Publisher
from doomtp_bot.runtime.engine import RunReport, Runtime
from doomtp_bot.runtime.result import Code
from doomtp_bot.runtime.values import MISSING
from doomtp_bot.runtime.variables import VarKey, WriteOp
from doomtp_bot.storage.db import Databases
from doomtp_bot.variables.access import Actor, VariableAccessPolicy, actor_of
from doomtp_bot.variables.store import SqliteVariableStore

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {
    "mod": {"id": "200", "name": "mod", "display": "Mod"},
    "alice": {"id": "400", "name": "alice", "display": "Alice"},
    "bob": {"id": "401", "name": "bob", "display": "Bob"},
    "carol": {"id": "402", "name": "carol", "display": "Carol"},
}
BADGES = {"mod": {"moderator"}}


async def resolve_user(login: str) -> dict[str, Any] | None:
    return next((dict(u) for u in USERS.values() if u["name"] == login), None)


async def login_for(user_id: str) -> str | None:
    return next((u["name"] for u in USERS.values() if u["id"] == user_id), None)


@dataclass
class TickingClock:
    """Moves forward a minute on every read, so per-user cooldowns never block back-to-back test messages."""

    now: float = 0.0

    def __call__(self) -> float:
        self.now += 60.0
        return self.now


@dataclass
class Harness:
    dbs: Databases
    policy: PolicyService
    store: SqliteVariableStore
    access: VariableAccessPolicy
    runtime: Runtime

    def chatter(self, who: str) -> Chatter:
        u = USERS[who]
        return self.policy.build_chatter(
            CHANNEL_ID, u["id"], u["name"], u["display"], frozenset(BADGES.get(who, set()))
        )

    def ctx(self, who: str | None, context: Context = Context.LINE, **kw: Any) -> ExecContext:
        channel = self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN)
        return self.runtime.make_context(
            channel=channel,
            invoker=self.chatter(who) if who else None,
            context=context,
            rng=random.Random(3),
            **kw,
        )

    async def run(self, who: str | None, text: str, context: Context = Context.LINE, **kw: Any) -> RunReport:
        publisher = kw.pop("publisher", None)
        report = await self.runtime.run(text, self.ctx(who, context, **kw), publisher=publisher)
        assert report is not None
        return report

    async def reply(self, who: str, text: str) -> str | None:
        return (await self.run(who, text)).send

    async def value(self, ns: str, key1: str, key2: str = "", key3: str = "", name: str = "") -> Any:
        return await self.store.get(VarKey(ns, key1, key2, key3, name))


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = PolicyService(dbs.bot, clock=TickingClock())
    await policy.reload()
    store = SqliteVariableStore(dbs.bot)
    access = VariableAccessPolicy(policy, dbs.bot)
    await access.reload()
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        store=store,
        access=access,
        resolve_user=resolve_user,
        services={"policy": policy, "variable_store": store, "login_for": login_for},
    )
    yield Harness(dbs, policy, store, access, runtime)


# ── store ──────────────────────────────────────────────────────────────────
async def test_store_commit_is_atomic_and_rolls_back(h: Harness) -> None:
    ctx = h.ctx("alice")
    key = VarKey("chatter", "400", name="n")
    await h.store.commit([WriteOp("set", key, 5), WriteOp("incr", key, 2)], ctx)
    assert await h.store.get(key) == 7
    with pytest.raises(Exception, match="not a list"):
        await h.store.commit([WriteOp("incr", key, 1), WriteOp("append", key, "x")], ctx)
    assert await h.store.get(key) == 7  # first op rolled back too


async def test_store_top_and_entries(h: Harness) -> None:
    ctx = h.ctx("mod")
    for user, points in (("400", 10), ("401", 30), ("402", 20)):
        await h.store.commit(
            [WriteOp("set", VarKey("channel.chatter", CHANNEL_ID, user, name="points"), points)], ctx
        )
    await h.store.commit(
        [WriteOp("set", VarKey("channel.chatter", CHANNEL_ID, "403", name="points"), "lots")], ctx
    )
    assert await h.store.top("channel.chatter", CHANNEL_ID, "", "points", 2) == [("401", 30), ("402", 20)]


# ── access matrix (variable-access-matrix.md §3) ───────────────────────────
MATRIX = [
    # (actor setup, namespace, expected can_write for a viewer)
    ("typed", "chatter", True),
    ("typed", "channel", False),
    ("typed", "channel.chatter", True),
    ("own_cc", "chatter", True),
    ("own_cc", "publisher.channel.chatter", True),
    ("foreign_link", "chatter", False),
    ("foreign_link", "channel.chatter", False),
    ("foreign_link", "publisher", True),
    ("foreign_link", "publisher.channel", True),
    ("foreign_pub", "channel", False),
    ("foreign_pub", "channel.chatter", False),
    ("foreign_pub", "publisher.chatter", True),
    ("trigger", "chatter", False),
    ("trigger", "channel.chatter", True),
    ("trigger", "publisher", False),
    ("callback", "channel.chatter", False),
    ("callback", "chatter", False),
]


def _actor_ctx(h: Harness, setup: str, who: str = "alice", **kw: Any) -> ExecContext:
    if setup == "typed":
        return h.ctx(who, **kw)
    if setup == "own_cc":
        return h.ctx(who, Context.BODY, publisher=Publisher(USERS[who]["id"], who), **kw)
    if setup == "foreign_link":
        return h.ctx(who, Context.BODY, publisher=Publisher("999", "someone"), **kw)
    if setup == "foreign_pub":
        return h.ctx(who, Context.BODY, publisher=Publisher("999", "someone", publication="pts"), **kw)
    if setup == "trigger":
        return h.ctx(who, Context.TRIGGER, trigger_type="redemption", **kw)
    return h.ctx(who, Context.CALLBACK, **kw)


@pytest.mark.parametrize(("setup", "namespace", "expected"), MATRIX)
async def test_access_matrix_for_viewer(h: Harness, setup: str, namespace: str, expected: bool) -> None:
    ctx = _actor_ctx(h, setup)
    assert actor_of(ctx) is Actor(setup)
    assert h.access.can_write(ctx, namespace, "x") is expected


async def test_channel_writes_follow_channel_var_write_role(h: Harness) -> None:
    assert h.access.can_write(h.ctx("mod"), "channel", "deaths") is True
    assert h.access.can_write(h.ctx("alice", Context.TRIGGER, run_as_rank=80), "channel", "deaths") is True
    assert h.access.can_write(h.ctx("alice", Context.TRIGGER, run_as_rank=0), "channel", "deaths") is False


async def test_publication_grants_are_exact(h: Harness) -> None:
    # Grants reference a publication (FK), so create a minimal custom command + publication first.
    await h.dbs.bot.execute(
        "INSERT INTO custom_commands (id, owner_user_id, name, current_version, created_at, updated_at)"
        " VALUES ('cc_1', '999', 'pts', 1, 0, 0)"
    )
    await h.dbs.bot.execute(
        "INSERT INTO custom_command_publications (channel_id, name, command_id, published_by, created_at)"
        " VALUES (?, 'pts', 'cc_1', '200', 0)",
        (CHANNEL_ID,),
    )
    await h.dbs.bot.commit()
    ctx = _actor_ctx(h, "foreign_pub")
    await h.access.set_grant(CHANNEL_ID, "pts", "channel.chatter.points", True, "200")
    assert h.access.can_write(ctx, "channel.chatter", "points") is True
    assert h.access.can_write(ctx, "channel.chatter", "coins") is False
    assert h.access.can_write(_actor_ctx(h, "foreign_link"), "channel.chatter", "points") is False
    with pytest.raises(ValueError):
        await h.access.set_grant(CHANNEL_ID, "pts", "channel.*", True, "200")
    await h.access.set_grant(CHANNEL_ID, "pts", "channel.chatter.points", False, "200")
    assert h.access.can_write(ctx, "channel.chatter", "points") is False


# ── store operator through the runtime ─────────────────────────────────────
async def test_store_operator_denied_for_viewer_on_channel(h: Harness) -> None:
    report = await h.run("alice", "!echo 1 > channel.deaths")
    assert (report.result.code, report.send) == (Code.DENIED, None)
    assert await h.value("channel", CHANNEL_ID, name="deaths") is MISSING
    report = await h.run("mod", "!echo 1 > channel.deaths")
    assert report.result.ok and await h.value("channel", CHANNEL_ID, name="deaths") == "1"


async def test_foreign_command_cannot_touch_invokers_chatter_vars(h: Harness) -> None:
    report = await h.run(
        "alice", "echo evil > chatter.location", Context.BODY, publisher=Publisher("999", "mallory")
    )
    assert report.result.code == Code.DENIED
    ok = await h.run(
        "alice", "echo 3 > publisher.chatter.save", Context.BODY, publisher=Publisher("999", "mallory")
    )
    assert ok.result.ok and await h.value("publisher.chatter", "999", "400", name="save") == "3"


# ── !var ───────────────────────────────────────────────────────────────────
async def test_var_set_get_incr_and_typed_values(h: Harness) -> None:
    assert await h.reply("alice", "!var set chatter.location Lisbon, PT") == "chatter.location = Lisbon, PT"
    assert await h.reply("alice", "!var get chatter.location") == "chatter.location = Lisbon, PT"
    assert await h.reply("alice", "!echo {chatter.location}") == "Lisbon, PT"
    assert await h.reply("mod", "!var set channel.goal 100") == "channel.goal = 100"
    assert await h.value("channel", CHANNEL_ID, name="goal") == 100
    assert await h.reply("mod", "!var incr channel.goal 2.5") == "channel.goal = 102.5"
    # `{` would start a placeholder in chat, so JSON lists are the practical typed-collection input.
    assert await h.reply("mod", "!var set channel.info [1, 2, 3]") == "channel.info = 1, 2, 3"
    assert await h.value("channel", CHANNEL_ID, name="info") == [1, 2, 3]
    assert await h.reply("mod", "!var get channel.info.1") == "channel.info.1 = 2"


async def test_var_writes_respect_matrix(h: Harness) -> None:
    assert await h.reply("alice", "!var set channel.goal 5") == "you can't change channel.goal"
    assert await h.reply("alice", "!var incr channel.chatter.points") == "channel.chatter.points = 1"
    assert await h.reply("alice", "!var set chatter.name x") is not None  # reserved name → usage error
    report = await h.run("alice", "!var set chatter.name x")
    assert report.result.code == Code.USAGE


async def test_everything_is_readable_including_other_users(h: Harness) -> None:
    await h.reply("bob", "!var set chatter.location Porto")
    assert await h.reply("alice", "!var get chatter.location @bob") == "chatter.location (@bob) = Porto"
    assert await h.reply("alice", "!var list chatter bob") == "location=Porto"


async def test_var_top_leaderboard(h: Harness) -> None:
    await h.reply("alice", "!var incr channel.chatter.points 10")
    await h.reply("bob", "!var incr channel.chatter.points 30")
    await h.reply("carol", "!var incr channel.chatter.points 20")
    assert await h.reply("alice", "!var top channel.chatter.points 2") == "1. bob 30, 2. carol 20"


async def test_var_delete_own_and_admin_reset(h: Harness) -> None:
    await h.reply("alice", "!var incr channel.chatter.points 5")
    assert await h.reply("bob", "!var del channel.chatter.points @alice") == (
        "you can't delete channel.chatter.points for Alice"
    )
    assert (
        await h.reply("mod", "!var del channel.chatter.points @alice")
        == "deleted channel.chatter.points for Alice"
    )
    assert await h.value("channel.chatter", CHANNEL_ID, "400", name="points") is MISSING
    await h.reply("alice", "!var set chatter.location here")
    assert await h.reply("alice", "!var del chatter.location") == "deleted chatter.location"
    async with h.dbs.bot.execute("SELECT action, target FROM audit_log ORDER BY id") as cur:
        audited = [(r[0], r[1]) for r in await cur.fetchall()]
    assert ("variable.delete", "channel.chatter.points@400") in audited  # admin reset of another user's row
    assert all(not t.startswith("chatter.") for _, t in audited)  # own chatter writes aren't audited


async def test_var_writes_are_part_of_the_run(h: Harness) -> None:
    report = await h.run("mod", "!var set channel.a 1 && var get channel.a && false")
    assert report.result.code == Code.FAIL
    assert await h.value("channel", CHANNEL_ID, name="a") == 1  # committed even though the line failed

    cancelled = await h.run("mod", "!var set channel.b 1 && echo x", is_cancelled=lambda: True)
    assert cancelled.result.code == Code.CANCELLED
    assert await h.value("channel", CHANNEL_ID, name="b") is MISSING


async def test_channel_writes_audited_with_values(h: Harness) -> None:
    await h.reply("mod", "!var set channel.deaths 3")
    await h.reply("mod", "!var incr channel.deaths")
    async with h.dbs.bot.execute(
        "SELECT action, before, after FROM audit_log WHERE target = 'channel.deaths'"
    ) as cur:
        rows = [(r[0], r[1], r[2]) for r in await cur.fetchall()]
    assert rows == [("variable.set", None, "3"), ("variable.incr", "3", "4")]
    assert json.loads(rows[1][2]) == 4
