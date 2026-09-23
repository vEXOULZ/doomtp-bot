"""Packs and derived (globally published) commands (ADR-0012)."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from doomtp_bot.customcmds.packs import PackService
from doomtp_bot.customcmds.resolution import CustomCommandLoader
from doomtp_bot.customcmds.service import CustomCommandError, CustomCommandService
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import RunReport, Runtime
from doomtp_bot.runtime.result import Code
from doomtp_bot.storage.db import Databases
from doomtp_bot.variables.access import VariableAccessPolicy
from doomtp_bot.variables.store import PostgresVariableStore
from tests.customcmds.test_customcmds import (
    BADGES,
    CHANNEL_ID,
    CHANNEL_LOGIN,
    USERS,
    TickingClock,
    resolve_user,
)

OTHER_CHANNEL = "200"


@dataclass
class Harness:
    dbs: Databases
    policy: PolicyService
    service: CustomCommandService
    packs: PackService
    runtime: Runtime

    async def run(self, who: str, text: str, channel: str = CHANNEL_ID) -> RunReport:
        info = self.policy.channel_info(channel, CHANNEL_LOGIN if channel == CHANNEL_ID else "other")
        info = dataclasses.replace(info, prefix="!")
        user = USERS[who]
        chatter = self.policy.build_chatter(
            channel, user["id"], user["name"], user["display"], frozenset(BADGES.get(who, set()))
        )
        report = await self.runtime.run(text, self.runtime.make_context(channel=info, invoker=chatter))
        assert report is not None
        return report

    async def say(self, who: str, text: str, channel: str = CHANNEL_ID) -> str | None:
        return (await self.run(who, text, channel)).send

    async def add(self, who: str, name: str, body: str) -> Any:
        user = USERS[who]
        return await self.service.create(
            owner_user_id=user["id"],
            owner_login=user["name"],
            name=name,
            body=body,
            channel_id=CHANNEL_ID,
            prefix="!",
        )


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = PolicyService(dbs.bot, bot_owner_ids=frozenset({USERS["owner"]["id"]}), clock=TickingClock())
    await policy.reload()
    for channel, login in ((CHANNEL_ID, CHANNEL_LOGIN), (OTHER_CHANNEL, "other")):
        await policy.mutate(lambda repo, c=channel, n=login: repo.ensure_channel(c, n, Actor(None, "system")))
    store = PostgresVariableStore(dbs.bot)
    access = VariableAccessPolicy(policy, dbs.bot)
    await access.reload()
    service = CustomCommandService(dbs.bot, on_grants_changed=access.reload)
    packs = PackService(dbs.bot, service)
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        store=store,
        access=access,
        resolve_user=resolve_user,
        custom=CustomCommandLoader(service, packs),
        services={
            "policy": policy,
            "variable_store": store,
            "customcmds": service,
            "packs": packs,
            "variable_access": access,
        },
    )
    yield Harness(dbs, policy, service, packs, runtime)


# ── packs ──────────────────────────────────────────────────────────────────
async def test_a_pack_publishes_all_its_commands_at_once(h: Harness) -> None:
    await h.say("alice", "!cc add hit echo hit me")
    await h.say("alice", "!cc add stand echo standing")
    assert await h.say("alice", "!cc pack create blackjack a card game") is not None
    assert await h.say("alice", "!cc pack add blackjack hit stand") == "hit, stand added to blackjack"
    assert await h.say("bob", "!hit") is None  # nothing published yet

    published = await h.say("mod", "!cc pack list")  # mod has no packs
    assert published == "your packs: none"
    await h.say("alice", "!cc pack share blackjack on")
    reply = await h.say("mod", "!cc publish pack @alice blackjack")
    assert reply is not None and "hit, stand" in reply
    assert await h.say("bob", "!hit") == "hit me"
    assert await h.say("bob", "!stand") == "standing"


async def test_commands_added_later_appear_immediately(h: Harness) -> None:
    await h.say("alice", "!cc add hit echo hit me")
    await h.say("alice", "!cc pack create blackjack")
    await h.say("alice", "!cc pack add blackjack hit")
    await h.say("alice", "!cc pack share blackjack on")
    await h.say("mod", "!cc publish pack @alice blackjack")

    await h.say("alice", "!cc add deal echo dealing")
    await h.say("alice", "!cc pack add blackjack deal")
    assert await h.say("bob", "!deal") == "dealing"  # no re-publish needed (ADR-0012)


async def test_a_pack_is_a_module_for_toggles(h: Harness) -> None:
    await h.say("alice", "!cc add hit echo hit me")
    await h.say("alice", "!cc pack create blackjack")
    await h.say("alice", "!cc pack add blackjack hit")
    await h.say("alice", "!cc pack share blackjack on")
    await h.say("mod", "!cc publish pack @alice blackjack")
    assert await h.say("bob", "!hit") == "hit me"

    assert await h.say("mod", "!module disable blackjack") == "blackjack disabled here"
    assert await h.say("bob", "!hit") is None  # disabled commands answer like unknown ones (spec §6.6)
    assert await h.say("mod", "!module enable blackjack") == "blackjack enabled here"
    assert await h.say("bob", "!hit") == "hit me"


async def test_unpublishing_a_pack_removes_every_member(h: Harness) -> None:
    await h.say("alice", "!cc add hit echo hit me")
    await h.say("alice", "!cc add stand echo standing")
    await h.say("alice", "!cc pack create blackjack")
    await h.say("alice", "!cc pack add blackjack hit stand")
    await h.say("alice", "!cc pack share blackjack on")
    await h.say("mod", "!cc publish pack @alice blackjack")
    assert await h.say("mod", "!cc unpublish pack @alice blackjack") is not None
    assert await h.say("bob", "!hit") is None
    assert await h.say("bob", "!stand") is None


async def test_publishing_a_pack_refuses_name_clashes_and_changes_nothing(h: Harness) -> None:
    await h.say("alice", "!cc add hit echo alice's hit")
    await h.say("alice", "!cc pack create blackjack")
    await h.say("alice", "!cc pack add blackjack hit")
    await h.say("mod", "!cc add hit echo mod's hit")
    await h.say("mod", "!cc publish hit")  # the channel already has a different "hit"

    await h.say("alice", "!cc pack share blackjack on")
    clash = await h.run("mod", "!cc publish pack @alice blackjack")
    assert clash.result.code == Code.USAGE and "hit" in (clash.result.message or "")
    assert await h.say("mod", "!hit") == "mod's hit"  # unchanged


async def test_pack_names_cannot_shadow_a_builtin_module(h: Harness) -> None:
    refused = await h.run("alice", "!cc pack create core_admin")
    assert refused.result.code == Code.USAGE and "built-in module" in (refused.result.message or "")


async def test_pack_info_lists_members_and_where_it_runs(h: Harness) -> None:
    await h.say("alice", "!cc add hit echo hit me")
    await h.say("alice", "!cc pack create blackjack")
    await h.say("alice", "!cc pack add blackjack hit")
    assert await h.say("alice", "!cc pack info blackjack") == "blackjack: hit — not published here"
    await h.say("alice", "!cc pack share blackjack on")
    await h.say("mod", "!cc publish pack @alice blackjack")
    assert await h.say("alice", "!cc pack info blackjack") == "blackjack: hit — here"


async def test_a_pack_holds_only_its_owners_commands(h: Harness) -> None:
    await h.say("bob", "!cc add hit echo bob's hit")
    await h.say("alice", "!cc pack create blackjack")
    refused = await h.run("alice", "!cc pack add blackjack hit")
    assert refused.result.code == Code.USAGE


# ── derived commands: published globally ───────────────────────────────────
async def test_a_global_publication_works_in_every_channel(h: Harness) -> None:
    await h.say("owner", "!cc add hug echo {chatter.display} hugs {arg.1 ?? everyone}")
    reply = await h.say("owner", "!cc publish hug global")
    assert reply is not None and "everywhere" in reply
    assert await h.say("bob", "!hug alice") == "Bob hugs alice"
    assert await h.say("bob", "!hug", channel=OTHER_CHANNEL) == "Bob hugs everyone"


async def test_only_bot_admins_publish_globally(h: Harness) -> None:
    await h.say("mod", "!cc add hug echo hugs")
    denied = await h.run("mod", "!cc publish hug global")
    assert (denied.result.code, denied.send) == (Code.DENIED, None)


async def test_a_channel_publication_wins_over_a_global_one(h: Harness) -> None:
    await h.say("owner", "!cc add hug echo global hug")
    await h.say("owner", "!cc publish hug global")
    await h.say("alice", "!cc add hug echo local hug")
    await h.say("alice", "!cc share hug on")
    await h.say("mod", "!cc link @alice hug")
    await h.say("mod", "!cc publish hug")
    assert await h.say("bob", "!hug") == "local hug"
    assert await h.say("bob", "!hug", channel=OTHER_CHANNEL) == "global hug"


async def test_a_global_pack_reaches_every_channel(h: Harness) -> None:
    await h.say("owner", "!cc add hit echo hit me")
    await h.say("owner", "!cc pack create blackjack")
    await h.say("owner", "!cc pack add blackjack hit")
    assert await h.say("owner", "!cc publish pack blackjack global") is not None
    assert await h.say("bob", "!hit", channel=OTHER_CHANNEL) == "hit me"
    assert await h.say("mod", "!module disable blackjack", channel=OTHER_CHANNEL) is not None
    assert await h.say("bob", "!hit", channel=OTHER_CHANNEL) is None  # a channel can still opt out


async def test_primitives_always_win(h: Harness) -> None:
    await h.say("owner", "!cc add ping echo not pong")
    await h.say("owner", "!cc publish ping global")
    assert await h.say("bob", "!ping") == "pong"


async def test_help_lists_pack_and_global_commands(h: Harness) -> None:
    await h.say("owner", "!cc add hug echo hugs")
    await h.say("owner", "!cc publish hug global")
    await h.say("alice", "!cc add hit echo hit me")
    await h.say("alice", "!cc pack create blackjack")
    await h.say("alice", "!cc pack add blackjack hit")
    await h.say("alice", "!cc pack share blackjack on")
    await h.say("mod", "!cc publish pack @alice blackjack")
    listing = await h.say("bob", "!help")
    assert listing is not None and "custom: hit, hug" in listing


async def test_duplicate_pack_names_per_owner(h: Harness) -> None:
    await h.packs.create(owner_user_id=USERS["alice"]["id"], name="blackjack")
    with pytest.raises(CustomCommandError, match="already have"):
        await h.packs.create(owner_user_id=USERS["alice"]["id"], name="blackjack")
    other = await h.packs.create(owner_user_id=USERS["bob"]["id"], name="blackjack")
    assert other.name == "blackjack"  # different owners may use the same name


async def test_global_scope_constant_is_the_policy_sentinel() -> None:
    assert GLOBAL == "*"


async def test_pack_writes_are_audited_with_the_source_they_came_from(h: Harness) -> None:
    alice, mod = USERS["alice"]["id"], USERS["mod"]["id"]
    hit = await h.add("alice", "hit", "echo hit me")
    pack = await h.packs.create(owner_user_id=alice, name="blackjack", actor_via="api")
    await h.packs.add_member(pack, hit, actor_via="api")
    await h.packs.publish(channel_id=CHANNEL_ID, pack=pack, published_by=mod, actor_via="api")
    await h.packs.unpublish(channel_id=CHANNEL_ID, pack=pack, actor_user_id=mod, actor_via="api")
    assert await h.packs.remove_member(pack, hit, actor_via="api")
    await h.packs.delete(pack, actor_via="api")
    await h.say("alice", "!cc pack create cards")

    async with await h.dbs.bot.execute(
        "SELECT action, via FROM audit_log WHERE action LIKE 'pack.%%' ORDER BY id"
    ) as cur:
        rows = [(r["action"], r["via"]) for r in await cur.fetchall()]
    assert rows == [
        ("pack.create", "api"),
        ("pack.add", "api"),
        ("pack.publish", "api"),
        ("pack.unpublish", "api"),
        ("pack.rm", "api"),
        ("pack.delete", "api"),
        ("pack.create", "chat"),  # chat still says chat
    ]
