"""Storage limits: quotas and value caps per namespace owner, `!admin quota|valuecap|listitems|names`, `!var usage` (ADR-0019)."""

from __future__ import annotations

import dataclasses
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from doomtp_bot.lang.parser import Context
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.context import ExecContext
from doomtp_bot.runtime.engine import RunReport, Runtime
from doomtp_bot.runtime.result import ErrorCode
from doomtp_bot.runtime.values import MISSING
from doomtp_bot.runtime.variables import (
    InMemoryVariableStore,
    Limits,
    VariableError,
    VarKey,
    WriteOp,
    format_size,
    owner_of,
    parse_size,
)
from doomtp_bot.storage.db import Databases
from doomtp_bot.variables.access import VariableAccessPolicy
from doomtp_bot.variables.store import PostgresVariableStore
from tests.fakes import TickingClock, policy_with_channels

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {
    "owner": {"id": "1", "name": "owner", "display": "Owner"},
    "doomtp": {"id": CHANNEL_ID, "name": "doomtp", "display": "Doomtp"},
    "alice": {"id": "400", "name": "alice", "display": "Alice"},
}


async def resolve_user(login: str) -> dict[str, Any] | None:
    return next((dict(u) for u in USERS.values() if u["name"] == login.lstrip("@")), None)


@dataclass
class Harness:
    store: PostgresVariableStore
    policy: PolicyService
    runtime: Runtime

    def ctx(self, who: str) -> ExecContext:
        u = USERS[who]
        channel = dataclasses.replace(self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN), prefix="!")
        chatter = self.policy.build_chatter(CHANNEL_ID, u["id"], u["name"], u["display"], frozenset())
        return self.runtime.make_context(
            channel=channel, invoker=chatter, context=Context.LINE, rng=random.Random(3)
        )

    async def run(self, who: str, text: str) -> RunReport:
        report = await self.runtime.run(text, self.ctx(who))
        assert report is not None
        return report


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = await policy_with_channels(dbs.bot, bot_owner_ids=frozenset({"1"}), clock=TickingClock())
    store = PostgresVariableStore(dbs.bot)
    access = VariableAccessPolicy(policy, dbs.bot)
    await access.reload()
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        store=store,
        access=access,
        resolve_user=resolve_user,
        services={"policy": policy, "variable_store": store},
    )
    yield Harness(store, policy, runtime)


# ── pure helpers ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "size"),
    [("4096", 4096), ("256KB", 262144), ("256k", 262144), ("1MB", 1048576), ("1.5mb", 1572864), ("x", None)],
)
def test_parse_size(text: str, size: int | None) -> None:
    assert parse_size(text) == size


def test_format_size() -> None:
    assert [format_size(n) for n in (12, 2048, 1572864)] == ["12 B", "2 KB", "1.5 MB"]


def test_every_namespace_counts_against_its_first_segment_and_key1() -> None:
    assert owner_of(VarKey("channel.chatter", "100", "400", name="x")) == ("channel", "100")
    assert owner_of(VarKey("publisher.channel.chatter", "7", "100", "400", name="x")) == ("publisher", "7")
    assert owner_of(VarKey("chatter", "400", name="x")) == ("chatter", "400")


async def test_the_in_memory_store_enforces_its_limits() -> None:
    store = InMemoryVariableStore(Limits(quota_bytes=20, value_cap_bytes=12))
    ctx: Any = None
    with pytest.raises(VariableError) as too_big:
        await store.commit([WriteOp("set", VarKey("chatter", "1", name="a"), "x" * 20)], ctx)
    assert too_big.value.code == ErrorCode.E_VALUE_TOO_BIG
    await store.commit([WriteOp("set", VarKey("chatter", "1", name="a"), "x" * 8)], ctx)  # 10 bytes
    with pytest.raises(VariableError) as full:
        await store.commit([WriteOp("set", VarKey("chatter", "1", name="b"), "x" * 9)], ctx)
    assert full.value.code == ErrorCode.E_QUOTA
    assert "b" not in {k.name for k in store.data}
    await store.commit([WriteOp("set", VarKey("chatter", "2", name="b"), "x" * 8)], ctx)  # another owner


# ── the Postgres store ──────────────────────────────────────────────────────
async def test_defaults_come_from_the_migration(h: Harness) -> None:
    assert await h.store.limits_for("channel", CHANNEL_ID) == Limits(1024 * 1024, 256 * 1024, 100, 200)


async def test_a_write_over_quota_rolls_the_whole_commit_back(h: Harness) -> None:
    await h.store.set_limit("chatter", "400", "quota_bytes", 35, actor="1", via="chat")
    ctx = h.ctx("alice")
    a, b = VarKey("chatter", "400", name="a"), VarKey("chatter", "400", name="b")
    await h.store.commit([WriteOp("set", a, "x" * 20)], ctx)
    with pytest.raises(VariableError) as exc:
        await h.store.commit([WriteOp("set", a, "short"), WriteOp("set", b, "x" * 30)], ctx)  # 7 + 32 bytes
    assert exc.value.code == ErrorCode.E_QUOTA
    assert await h.store.get(a) == "x" * 20
    assert await h.store.get(b) is MISSING
    # Another owner and the channel's own space are unaffected.
    await h.store.commit([WriteOp("set", VarKey("chatter", "401", name="b"), "x" * 20)], ctx)


async def test_shrinking_is_allowed_when_already_over_quota(h: Harness) -> None:
    ctx = h.ctx("alice")
    key = VarKey("chatter", "400", name="a")
    await h.store.commit([WriteOp("set", key, "x" * 40)], ctx)
    await h.store.set_limit("chatter", "400", "quota_bytes", 10, actor="1", via="chat")
    await h.store.commit([WriteOp("set", key, "x" * 30)], ctx)
    assert (await h.store.usage("chatter", "400")) == {"chatter": 32}


async def test_the_value_cap_is_per_owner(h: Harness) -> None:
    await h.store.set_limit("channel", CHANNEL_ID, "value_cap_bytes", 10, actor="1", via="chat")
    ctx = h.ctx("alice")
    with pytest.raises(VariableError) as exc:
        await h.store.commit(
            [WriteOp("set", VarKey("channel.chatter", CHANNEL_ID, "400", name="n"), "x" * 10)], ctx
        )
    assert exc.value.code == ErrorCode.E_VALUE_TOO_BIG
    await h.store.commit([WriteOp("set", VarKey("chatter", "400", name="n"), "x" * 10)], ctx)


async def test_an_override_falls_back_to_the_default_field_by_field(h: Harness) -> None:
    await h.store.set_limit("publisher", "7", "quota_bytes", 2048, actor="1", via="chat")
    assert await h.store.limits_for("publisher", "7") == Limits(2048, 256 * 1024)
    await h.store.set_limit("publisher", "7", "quota_bytes", None, actor="1", via="chat")
    assert await h.store.override("publisher", "7") is None
    with pytest.raises(ValueError):
        await h.store.set_limit("*", "*", "quota_bytes", None, actor="1", via="chat")


# ── commands ────────────────────────────────────────────────────────────────
async def test_admin_quota_sets_shows_and_resets(h: Harness) -> None:
    assert (
        await h.run("owner", "!admin quota channel doomtp")
    ).send == "channel Doomtp quota: 1 MB (default)"
    assert (
        await h.run("owner", "!admin quota channel doomtp 2MB")
    ).send == "channel Doomtp quota is now 2 MB"
    assert await h.store.limits_for("channel", CHANNEL_ID) == Limits(2 * 1024 * 1024, 256 * 1024)
    assert (await h.run("owner", "!admin quota channel doomtp")).send == "channel Doomtp quota: 2 MB"
    assert (await h.run("owner", "!admin quota channel doomtp reset")).send == (
        "channel Doomtp quota is back to the default"
    )
    assert (await h.run("owner", "!admin valuecap default 64KB")).send == "default value cap is now 64 KB"
    assert (await h.run("owner", "!admin valuecap default")).send == "default value cap: 64 KB"


async def test_admin_quota_refuses_bad_sizes_and_non_owners(h: Harness) -> None:
    assert "at most 1 MB" in ((await h.run("owner", "!admin valuecap default 2MB")).send or "")
    assert "at most" in ((await h.run("owner", "!admin quota chatter alice lots")).send or "")
    assert "can't be reset" in ((await h.run("owner", "!admin quota default reset")).send or "")
    denied = await h.run("alice", "!admin quota default 1KB")
    assert denied.result is not None and not denied.result.ok
    assert await h.store.limits_for("chatter", "400") == Limits()


async def test_var_usage_counts_every_namespace_of_the_owner(h: Harness) -> None:
    ctx = h.ctx("alice")
    await h.store.commit(
        [
            WriteOp("set", VarKey("channel", CHANNEL_ID, name="a"), "x" * 98),
            WriteOp("set", VarKey("channel.chatter", CHANNEL_ID, "400", name="b"), "x" * 2046),
        ],
        ctx,
    )
    report = await h.run("alice", "!var usage channel.chatter")
    assert report.send == "channel storage: 2.1 KB of 1 MB (0%) — channel 100 B, channel.chatter 2 KB"
    assert report.result is not None and report.result.data == {
        "owner": "channel",
        "used": 2148,
        "quota": 1024 * 1024,
        "namespaces": {"channel": 100, "channel.chatter": 2048},
    }
    assert (await h.run("alice", "!var usage")).send == "chatter storage: 0 B of 1 MB (0%)"


async def test_a_run_over_quota_fails_with_e_quota(h: Harness) -> None:
    await h.store.set_limit("chatter", "400", "quota_bytes", 16, actor="1", via="chat")
    report = await h.run("alice", "!var set chatter.note this note is much too long")
    assert report.result is not None and report.result.code == ErrorCode.E_QUOTA
    assert await h.store.get(VarKey("chatter", "400", name="note")) is MISSING


async def test_admin_sets_the_list_and_name_limits_as_counts(h: Harness) -> None:
    assert (await h.run("owner", "!admin listitems default")).send == "default list limit: 100"
    assert (
        await h.run("owner", "!admin listitems channel doomtp 3")
    ).send == "channel Doomtp list limit is now 3"
    assert (
        await h.run("owner", "!admin names chatter alice 2")
    ).send == "chatter Alice variable limit is now 2"
    assert await h.store.limits_for("chatter", "400") == Limits(names_per_space=2)
    assert "whole number" in ((await h.run("owner", "!admin names default 1KB")).send or "")
    assert "at most 10000" in ((await h.run("owner", "!admin listitems default 20000")).send or "")
    assert (await h.run("owner", "!admin names chatter alice reset")).send == (
        "chatter Alice variable limit is back to the default"
    )
    assert await h.store.override("chatter", "400") is None


async def test_a_list_limit_override_applies_to_that_owner_only(h: Harness) -> None:
    await h.store.set_limit("channel", CHANNEL_ID, "list_items", 2, actor="1", via="chat")
    await h.run("owner", "!echo a --> channel.log")
    await h.run("owner", "!echo b --> channel.log")
    report = await h.run("owner", "!echo c --> channel.log")
    assert report.result is not None and report.result.code == ErrorCode.E_LIST_FULL
    assert await h.store.get(VarKey("channel", CHANNEL_ID, name="log")) == ["a", "b"]
    await h.run("alice", "!echo a --> chatter.log")
    await h.run("alice", "!echo b --> chatter.log")
    assert (await h.run("alice", "!echo c --> chatter.log")).result.ok  # type: ignore[union-attr]


async def test_a_name_limit_override_caps_the_variables_in_a_space(h: Harness) -> None:
    await h.store.set_limit("chatter", "400", "names_per_space", 1, actor="1", via="chat")
    assert (await h.run("alice", "!var set chatter.one 1")).result.ok  # type: ignore[union-attr]
    assert (await h.run("alice", "!var set chatter.one 2")).result.ok  # type: ignore[union-attr]
    report = await h.run("alice", "!var set chatter.two 1")
    assert report.result is not None and report.result.code == ErrorCode.E_TOO_MANY_NAMES
