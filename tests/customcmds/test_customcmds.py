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
from doomtp_bot.variables.store import PostgresVariableStore

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {
    "alice": {"id": "400", "name": "alice", "display": "Alice"},
    "bob": {"id": "401", "name": "bob", "display": "Bob"},
    "mod": {"id": "300", "name": "mod", "display": "Mod"},
    "owner": {"id": "1", "name": "owner", "display": "Owner"},
}
BADGES = {"mod": {"moderator"}}


class TickingClock:
    """Monotonic clock that jumps a minute per read, so per-user cooldowns never block a test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 60.0
        return self.now


async def resolve_user(login: str) -> dict[str, Any] | None:
    return next((dict(u) for u in USERS.values() if u["name"] == login.lower()), None)


@dataclass
class Harness:
    dbs: Databases
    policy: PolicyService
    service: CustomCommandService
    store: PostgresVariableStore
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
    policy = PolicyService(dbs.bot, clock=TickingClock())
    await policy.reload()
    await policy.mutate(lambda repo: repo.ensure_channel(CHANNEL_ID, CHANNEL_LOGIN, Actor(None, "system")))
    store = PostgresVariableStore(dbs.bot)
    access = VariableAccessPolicy(policy, dbs.bot)
    await access.reload()
    service = CustomCommandService(dbs.bot, on_grants_changed=access.reload)
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        store=store,
        access=access,
        resolve_user=resolve_user,
        custom=CustomCommandLoader(service),
        services={
            "policy": policy,
            "variable_store": store,
            "customcmds": service,
            "variable_access": access,
        },
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


async def test_publishing_says_which_writes_still_need_a_grant(h: Harness) -> None:
    """ADR-0010: a mod publishing a community command shouldn't find out from silence (§4)."""
    await h.add("mod", "count", "echo 1 > channel.deaths | echo {_} >> channel.chatter.log")
    published = await h.run("mod", "!cc publish count")
    assert "count writes channel.chatter.log, channel.deaths" in published.send
    assert "!cc grant count channel.chatter.log" in published.send

    command = await h.service.by_owner(USERS["mod"]["id"], "count")
    assert command is not None
    for variable in ("channel.deaths", "channel.chatter.log"):
        await h.access.set_grant(CHANNEL_ID, command.id, variable, True, USERS["mod"]["id"])
    again = await h.run("mod", "!cc publish count")  # publishing again keeps the grants
    assert "grant" not in again.send  # everything it writes is already allowed

    await h.add("mod", "quiet", "echo hi > chatter.note")
    assert "grant" not in (await h.run("mod", "!cc publish quiet")).send  # its own variables, no grant


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


# ── the !cc chat commands ──────────────────────────────────────────────────
async def test_cc_add_publish_link_and_run_from_chat(h: Harness) -> None:
    created = await h.say("alice", "!cc add hype echo {chatter.display} is hyped!")
    assert created is not None and created.startswith("created !hype (cc_")
    assert await h.say("alice", "!hype") == "Alice is hyped!"

    # A viewer can't publish; a moderator can, and the reply warns about live edits.
    denied = await h.run("bob", "!cc publish hype")
    assert (denied.result.code, denied.send) == (Code.DENIED, None)

    hype = await h.service.by_owner(USERS["alice"]["id"], "hype")
    assert hype is not None
    await h.service.link(user_id=USERS["mod"]["id"], alias="hype", command=hype)
    published = await h.say("mod", "!cc publish hype")
    assert published is not None and "⚠ @alice can edit or delete it" in published
    assert await h.say("bob", "!hype") == "Bob is hyped!"


async def test_cc_link_warns_and_respects_sharing(h: Harness) -> None:
    await h.say("alice", "!cc add hi echo hi from {publisher.name}")
    refused = await h.run("bob", "!cc link @alice hi")
    assert refused.result.code == Code.USAGE  # not shared yet
    assert await h.say("alice", "!cc share hi on") is not None

    linked = await h.say("bob", "!cc link @alice hi mine")
    assert linked is not None and "⚠ @alice can edit or delete it" in linked
    assert await h.say("bob", "!mine") == "hi from alice"
    assert await h.say("bob", "!cc unlink mine") == "unlinked mine"


async def test_cc_edit_reports_reach_and_rejects_broken_bodies(h: Harness) -> None:
    await h.say("alice", "!cc add hi echo one")
    broken = await h.run("alice", "!cc edit hi echo a ; b")
    assert broken.result.code == Code.USAGE and "E_RESERVED_OPERATOR" in (broken.result.message or "")
    assert await h.say("alice", "!hi") == "one"  # unchanged

    assert await h.say("alice", "!cc edit hi echo two") == "hi is now v2, live in 1 alias, 0 channels"
    assert await h.say("alice", "!hi") == "two"


async def test_cc_grant_is_moderator_only_and_scoped_to_the_command(h: Harness) -> None:
    await h.say("alice", "!cc add count echo 1 > channel.deaths")
    await h.say("alice", "!cc share count on")
    await h.say("mod", "!cc link @alice count")
    await h.say("mod", "!cc publish count")
    assert (await h.run("bob", "!count")).result.code == Code.DENIED

    viewer = await h.run("bob", "!cc grant count channel.deaths")
    assert viewer.result.code == Code.DENIED
    granted = await h.say("mod", "!cc grant count channel.deaths")
    assert granted is not None and "after future edits by @alice" in granted
    assert (await h.run("bob", "!count")).result.ok

    assert await h.say("mod", "!cc revoke count channel.deaths") == "count can no longer write channel.deaths"
    assert (await h.run("bob", "!count")).result.code == Code.DENIED


async def test_cc_unpublish_revokes_grants(h: Harness) -> None:
    await h.say("alice", "!cc add count echo 1 > channel.deaths")
    await h.say("alice", "!cc share count on")
    await h.say("mod", "!cc link @alice count")
    await h.say("mod", "!cc publish count")
    await h.say("mod", "!cc grant count channel.deaths")
    assert await h.say("mod", "!cc unpublish count") is not None

    await h.say("mod", "!cc publish count")  # same command, published again
    assert (await h.run("bob", "!count")).result.code == Code.DENIED  # the grant did not come back


async def test_cc_list_and_info(h: Harness) -> None:
    await h.say("alice", "!cc add hi echo hi")
    assert await h.say("alice", "!cc list") == "yours: hi"
    info = await h.say("alice", "!cc info hi")
    assert info is not None and "by @alice, v1" in info and "echo hi" in info


# ── running by id (ADR-0009 action item 6) ─────────────────────────────────
async def test_cc_run_reaches_a_command_by_id(h: Harness) -> None:
    """The owner's escape hatch: no publication, no alias, and the name is a built-in's."""
    await h.say("alice", "!cc add ping echo pong from {publisher.name} to {arg.1 ?? nobody}")
    assert await h.say("alice", "!ping") == "pong"  # the built-in still wins by name
    command = await h.service.by_owner(USERS["alice"]["id"], "ping")
    assert command is not None

    assert await h.say("alice", f"!cc run {command.id} bob") == "pong from alice to bob"
    assert await h.say("alice", f"!cc run {command.id}") == "pong from alice to nobody"


async def test_cc_run_refuses_a_private_command_and_an_unknown_id(h: Harness) -> None:
    await h.say("alice", "!cc add secret echo the password is hunter2")
    command = await h.service.by_owner(USERS["alice"]["id"], "secret")
    assert command is not None

    private = await h.run("bob", f"!cc run {command.id}")
    assert (private.result.code, private.send) == (Code.DENIED, None)

    await h.say("alice", "!cc share secret on")  # shared: the same door cc link opens
    assert await h.say("bob", f"!cc run {command.id}") == "the password is hunter2"

    unknown = await h.run("bob", "!cc run cc_nope")
    assert unknown.result.code == Code.USAGE and "no command with id" in (unknown.result.message or "")


async def test_cc_run_validates_declared_params_and_runs_as_the_invoker(h: Harness) -> None:
    await h.say("alice", "!cc add roll echo {chatter.display} rolled {arg.sides}")
    await h.say("alice", '!cc param roll 1 name=sides type=int min=2 max=100 required=yes "sides"')
    await h.say("alice", "!cc share roll on")
    command = await h.service.by_owner(USERS["alice"]["id"], "roll")
    assert command is not None

    assert await h.say("bob", f"!cc run {command.id} 20") == "Bob rolled 20"
    too_big = await h.run("bob", f"!cc run {command.id} 500")
    assert too_big.result.code == Code.USAGE and "sides" in (too_big.result.message or "")


async def test_a_body_cannot_call_cc_run(h: Harness) -> None:
    """Otherwise a body could recurse past the depth and cycle checks preflight does for names."""
    inner = await h.add("alice", "inner", "echo inner")
    await h.say("alice", f"!cc add outer cc run {inner.id}")
    report = await h.run("alice", "!outer")
    assert report.result.code == Code.USAGE and "typed in chat" in (report.result.message or "")


# ── declared parameters and !help (ADR-0009 action item 3) ─────────────────
async def test_declared_params_are_validated_and_shown(h: Harness) -> None:
    await h.say("alice", "!cc add roll random 1-{arg.sides} | echo {chatter.display} rolled {1}")
    usage = await h.say(
        "alice", '!cc param roll 1 name=sides type=int min=2 max=100 required=yes "how many sides"'
    )
    assert usage == "!roll <sides> — 1 sides: int — how many sides"

    good = await h.run("alice", "!roll 20")
    assert good.result.ok and good.send is not None and good.send.startswith("Alice rolled ")

    too_big = await h.run("alice", "!roll 500")
    assert too_big.result.code == Code.USAGE and "sides" in (too_big.result.message or "")
    missing = await h.run("alice", "!roll")
    assert missing.result.code == Code.USAGE and "required" in (missing.result.message or "")

    assert await h.say("alice", "!cc param roll 1 remove") == "!roll [arguments…] — takes free arguments"


async def test_param_declaration_is_rejected_when_malformed(h: Harness) -> None:
    await h.say("alice", "!cc add pick echo {arg.choice}")
    bad_type = await h.run("alice", "!cc param pick 1 name=choice type=colour")
    assert bad_type.result.code == Code.USAGE and "type must be one of" in (bad_type.result.message or "")
    gap = await h.run("alice", "!cc param pick 2 name=second")
    assert gap.result.code == Code.USAGE and "1, 2, 3" in (gap.result.message or "")


async def test_help_lists_custom_commands_the_caller_can_run(h: Harness) -> None:
    await h.say("alice", "!cc add hype echo hyped")
    await h.say("alice", "!cc describe hype gets the chat hyped")
    await h.say("mod", "!cc link @alice hype")  # not shared yet: fails, so share first
    await h.say("alice", "!cc share hype on")
    await h.say("mod", "!cc link @alice hype")
    await h.say("mod", "!cc publish hype")

    listing = await h.say("bob", "!help")
    assert listing is not None and "custom: hype" in listing
    detail = await h.say("bob", "!help hype")
    assert (
        detail
        == "!hype [arguments…] — gets the chat hyped — 1+ arguments: str (optional) — passed to the command body"
    )

    alices = await h.say("alice", "!help")
    assert alices is not None and "custom: hype" in alices  # her own alias
