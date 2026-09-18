"""Custom commands end to end: create, link, publish, run, edit, delete (ADR-0009)."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from doomtp_bot.customcmds.resolution import CustomCommandLoader
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.lang.parser import Context
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import RunReport, Runtime
from doomtp_bot.runtime.result import Code
from doomtp_bot.runtime.values import MISSING
from doomtp_bot.runtime.variables import VarKey
from doomtp_bot.storage.db import Databases
from doomtp_bot.variables.access import VariableAccessPolicy
from doomtp_bot.variables.store import SqliteVariableStore

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {
    "alice": {"id": "400", "name": "alice", "display": "Alice"},
    "bob": {"id": "401", "name": "bob", "display": "Bob"},
    "mod": {"id": "300", "name": "mod", "display": "Mod"},
}
BADGES = {"mod": {"moderator"}}


async def resolve_user(login: str) -> dict[str, Any] | None:
    return next((dict(u) for u in USERS.values() if u["name"] == login.lower()), None)


@dataclass
class Harness:
    dbs: Databases
    policy: PolicyService
    service: CustomCommandService
    store: SqliteVariableStore
    access: VariableAccessPolicy
    runtime: Runtime

    def chatter(self, who: str) -> Any:
        u = USERS[who]
        return self.policy.build_chatter(
            CHANNEL_ID, u["id"], u["name"], u["display"], frozenset(BADGES.get(who, set()))
        )

    async def run(self, who: str, text: str) -> RunReport:
        channel = self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN)
        channel = dataclasses.replace(channel, prefix="!")
        report = await self.runtime.run(
            text, self.runtime.make_context(channel=channel, invoker=self.chatter(who))
        )
        assert report is not None
        return report

    async def say(self, who: str, text: str) -> str | None:
        return (await self.run(who, text)).send

    async def add(self, who: str, name: str, body: str) -> Any:
        user = USERS[who]
        return await self.service.create(
            owner_user_id=user["id"], owner_login=user["name"], name=name, body=body
        )

    async def value(self, ns: str, key1: str, key2: str = "", key3: str = "", name: str = "") -> Any:
        return await self.store.get(VarKey(ns, key1, key2, key3, name))


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = PolicyService(dbs.bot)
    await policy.reload()
    await policy.mutate(lambda repo: repo.ensure_channel(CHANNEL_ID, CHANNEL_LOGIN, Actor(None, "system")))
    service = CustomCommandService(dbs.bot)
    store = SqliteVariableStore(dbs.bot)
    access = VariableAccessPolicy(policy, dbs.bot)
    await access.reload()
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        store=store,
        access=access,
        resolve_user=resolve_user,
        custom=CustomCommandLoader(service),
        services={"policy": policy, "variable_store": store},
    )
    yield Harness(dbs, policy, service, store, access, runtime)


# ── personal aliases and publications ──────────────────────────────────────
async def test_owner_runs_their_own_command(h: Harness) -> None:
    await h.add("alice", "hi", "echo hello {chatter.display}, you said {arg.1 ?? nothing}")
    assert await h.say("alice", "!hi there") == "hello Alice, you said there"
    assert await h.say("alice", "!hi") == "hello Alice, you said nothing"
    assert await h.say("bob", "!hi") is None  # not published, not linked: unknown command, silent


async def test_publication_serves_the_whole_channel(h: Harness) -> None:
    command = await h.add("alice", "roll", "random 1-{arg.1 ?? 20} | echo {chatter.display} rolled {1}")
    await h.service.publish(
        channel_id=CHANNEL_ID, name="roll", command=command, published_by=USERS["mod"]["id"]
    )
    reply = await h.say("bob", "!roll 6")
    assert reply is not None and reply.startswith("Bob rolled ")


async def test_link_gives_a_personal_alias_and_unlink_removes_it(h: Harness) -> None:
    command = await h.add("alice", "hi", "echo hi from {publisher.name}")
    await h.service.link(user_id=USERS["bob"]["id"], alias="yo", command=command)
    assert await h.say("bob", "!yo") == "hi from alice"
    assert await h.say("bob", "!@yo") == "hi from alice"  # @ addresses the personal alias explicitly
    assert await h.service.unlink(user_id=USERS["bob"]["id"], alias="yo") is True
    assert await h.say("bob", "!yo") is None


async def test_builtins_win_over_publications(h: Harness) -> None:
    command = await h.add("alice", "ping", "echo custom")
    await h.service.publish(
        channel_id=CHANNEL_ID, name="ping", command=command, published_by=USERS["mod"]["id"]
    )
    assert await h.say("bob", "!ping") == "pong"


# ── live edits and deletes ─────────────────────────────────────────────────
async def test_edits_are_live_and_deletes_break_links_immediately(h: Harness) -> None:
    command = await h.add("alice", "hi", "echo one")
    await h.service.link(user_id=USERS["bob"]["id"], alias="hi", command=command)
    assert await h.say("bob", "!hi") == "one"

    edited = await h.service.edit(command, "echo two")
    assert edited.version == 2
    assert await h.say("bob", "!hi") == "two"

    reverted = await h.service.revert(edited, 1)
    assert (reverted.version, await h.say("bob", "!hi")) == (3, "one")

    links, publications = await h.service.delete(reverted)
    assert (links, publications) == (2, 0)  # alice's own alias plus bob's link
    assert await h.say("bob", "!hi") is None


async def test_deleting_orphans_publications(h: Harness) -> None:
    command = await h.add("alice", "roll", "echo rolled")
    await h.service.publish(
        channel_id=CHANNEL_ID, name="roll", command=command, published_by=USERS["mod"]["id"]
    )
    await h.service.delete(command)
    assert await h.say("bob", "!roll") is None
    rows = await h.service.publications_in(CHANNEL_ID)
    assert [(p.name, p.status) for p, _ in rows] == [("roll", "orphaned")]


async def test_mods_can_disable_and_re_enable_a_publication(h: Harness) -> None:
    command = await h.add("alice", "roll", "echo rolled")
    await h.service.publish(
        channel_id=CHANNEL_ID, name="roll", command=command, published_by=USERS["mod"]["id"]
    )
    await h.service.set_publication_status(
        channel_id=CHANNEL_ID, name="roll", status="disabled", actor_user_id=USERS["mod"]["id"]
    )
    assert await h.say("bob", "!roll") is None
    await h.service.set_publication_status(
        channel_id=CHANNEL_ID, name="roll", status="active", actor_user_id=USERS["mod"]["id"]
    )
    assert await h.say("bob", "!roll") == "rolled"


# ── limits (spec §5.2 checks 7 and 8) ──────────────────────────────────────
async def test_a_command_calling_itself_fails_preflight(h: Harness) -> None:
    command = await h.add("alice", "loop", "echo start")
    await h.service.edit(command, "loop")
    report = await h.run("alice", "!loop")
    assert report.result.code == Code.USAGE
    assert isinstance(report.result.data, dict) and report.result.data["error"] == "E_CC_CYCLE"


async def test_nesting_deeper_than_the_limit_fails_preflight(h: Harness) -> None:
    await h.add("alice", "d4", "echo bottom")
    for level in (3, 2, 1):
        await h.add("alice", f"d{level}", f"d{level + 1}")
    report = await h.run("alice", "!d1")
    assert isinstance(report.result.data, dict) and report.result.data["error"] == "E_CC_DEPTH"


async def test_expanded_invocations_count_toward_the_limit(h: Harness) -> None:
    await h.add("alice", "five", "echo a && echo b && echo c && echo d && echo e")
    report = await h.run("alice", "!five && echo x && echo y && echo z")
    assert isinstance(report.result.data, dict) and report.result.data["error"] == "E_TOO_MANY"


# ── identity and variable access (the safety rules) ────────────────────────
async def test_the_body_runs_as_the_invoker_not_the_owner(h: Harness) -> None:
    """A moderator's command run by a viewer stays a viewer's run (ADR-0009)."""
    command = await h.add("mod", "mine", "echo {chatter.display} rank {chatter.rank}")
    await h.service.publish(
        channel_id=CHANNEL_ID, name="mine", command=command, published_by=USERS["mod"]["id"]
    )
    assert await h.say("bob", "!mine") == "Bob rank 0"
    assert await h.say("mod", "!mine") == "Mod rank 80"


async def test_a_published_body_cannot_write_channel_variables_without_a_grant(h: Harness) -> None:
    command = await h.add("alice", "count", "echo 1 > channel.deaths")
    await h.service.publish(
        channel_id=CHANNEL_ID, name="count", command=command, published_by=USERS["mod"]["id"]
    )
    denied = await h.run("bob", "!count")
    assert (denied.result.code, denied.send) == (Code.DENIED, None)
    assert await h.value("channel", CHANNEL_ID, name="deaths") is MISSING

    await h.access.set_grant(CHANNEL_ID, command.id, "channel.deaths", True, USERS["mod"]["id"])
    allowed = await h.run("bob", "!count")
    assert allowed.result.ok
    assert await h.value("channel", CHANNEL_ID, name="deaths") == "1"


async def test_publisher_variables_belong_to_the_owner(h: Harness) -> None:
    command = await h.add("alice", "note", "echo {arg.1} > publisher.channel.note")
    await h.service.publish(
        channel_id=CHANNEL_ID, name="note", command=command, published_by=USERS["mod"]["id"]
    )
    assert (await h.run("bob", "!note hello")).result.ok
    assert await h.value("publisher.channel", USERS["alice"]["id"], CHANNEL_ID, name="note") == "hello"


async def test_publisher_is_restored_after_a_nested_body(h: Harness) -> None:
    """A nested command must not leave its publisher behind for the rest of the line."""
    inner = await h.add("alice", "inner", "echo {publisher.name}")
    await h.service.publish(
        channel_id=CHANNEL_ID, name="inner", command=inner, published_by=USERS["mod"]["id"]
    )
    outer = await h.add("bob", "outer", "inner | echo inner said {1}, mine is {publisher.name}")
    await h.service.publish(
        channel_id=CHANNEL_ID, name="outer", command=outer, published_by=USERS["mod"]["id"]
    )
    assert await h.say("mod", "!outer") == "inner said alice, mine is bob"


async def test_quota_and_duplicate_names(h: Harness) -> None:
    from doomtp_bot.customcmds.service import CustomCommandError

    h.service.quota = 2
    await h.add("alice", "one", "echo 1")
    with pytest.raises(CustomCommandError, match="already have"):
        await h.add("alice", "one", "echo 2")
    await h.add("alice", "two", "echo 2")
    with pytest.raises(CustomCommandError, match="limit of 2"):
        await h.add("alice", "three", "echo 3")


async def test_bodies_are_parsed_in_body_context(h: Harness) -> None:
    """`{arg.*}` is only available inside a body, so a body may use it and a typed line may not."""
    await h.add("alice", "args", "echo you said {arg.1+ ?? nothing}")
    assert await h.say("alice", "!args a b c") == "you said a b c"
    typed = await h.run("alice", "!echo {arg.1}")
    assert isinstance(typed.result.data, dict) and typed.result.data["error"] == "E_BAD_REFERENCE"
    assert h.service.parse_body("echo ok", "!") is not None
    assert Context.BODY is Context("body")
