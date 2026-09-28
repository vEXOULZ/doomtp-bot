"""Internal pack members and system packs (ADR-0019 "Sentinels", ADR-0012 amendment)."""

from __future__ import annotations

import pytest

from doomtp_bot.customcmds import system
from doomtp_bot.customcmds.packs import SystemPackError
from doomtp_bot.customcmds.resolution import SystemResolver
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.customcmds.system import (
    CORE,
    CORE_COMMANDS,
    CORE_VERSION,
    CoreNotInstalled,
    NotASentinel,
    check_sentinel_body,
    require_core,
)
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.runtime.result import Code
from scripts.starter_pack import check_core, install
from tests.customcmds.test_customcmds import USERS
from tests.customcmds.test_packs import OTHER_CHANNEL, Harness, h  # noqa: F401

OWNER = USERS["owner"]


async def _blackjack(h: Harness) -> None:  # noqa: F811
    await h.say("alice", "!cc add deal echo dealt {arg.1 ?? a card}")
    await h.say("alice", "!cc add hit deal ace")
    await h.say("alice", "!cc pack create blackjack")
    await h.say("alice", "!cc pack add blackjack hit deal")
    await h.say("alice", "!cc pack share blackjack on")
    await h.say("mod", "!cc publish pack @alice blackjack")


async def _install_core(h: Harness) -> None:  # noqa: F811
    await install(h.dbs.bot, owner_user_id=OWNER["id"], owner_login=OWNER["name"])
    resolver = h.runtime.resolver
    assert isinstance(resolver, SystemResolver)
    await resolver.reload(h.packs)


# ── internal members ─────────────────────────────────────────────────────────
async def test_an_internal_member_runs_only_from_its_packs_bodies(h: Harness) -> None:  # noqa: F811
    await _blackjack(h)
    assert await h.say("bob", "!deal") == "dealt a card"
    reply = await h.say("alice", "!cc pack internal blackjack deal on")
    assert reply == "deal in blackjack now internal: only its commands can call them"

    assert await h.say("bob", "!hit") == "dealt ace"  # its sibling still reaches it
    typed = await h.run("bob", "!deal")
    assert typed.result.code == Code.UNKNOWN  # typed, it doesn't exist
    assert (await h.run("bob", "!echo x | deal")).result.code == Code.UNKNOWN

    await h.say("alice", "!cc pack internal blackjack deal off")
    assert await h.say("bob", "!deal") == "dealt a card"


async def test_a_channel_publication_cannot_shadow_an_internal_member(h: Harness) -> None:  # noqa: F811
    await _blackjack(h)
    await h.say("alice", "!cc pack internal blackjack deal on")
    await h.say("mod", "!cc add deal echo the channel's own deal")
    await h.say("mod", "!cc publish deal")

    assert await h.say("bob", "!deal") == "the channel's own deal"
    assert await h.say("bob", "!hit") == "dealt ace"  # the pack keeps calling its own helper


async def test_internal_members_are_hidden_from_help_and_marked_in_info(h: Harness) -> None:  # noqa: F811
    await _blackjack(h)
    await h.say("alice", "!cc pack internal blackjack deal on")

    listing = await h.say("bob", "!help")
    assert listing is not None and "custom: hit" in listing and "deal" not in listing
    assert await h.say("alice", "!cc pack info blackjack") == "blackjack: deal (internal), hit — here"


async def test_only_members_can_be_made_internal(h: Harness) -> None:  # noqa: F811
    await _blackjack(h)
    await h.say("alice", "!cc add stand echo standing")
    reply = await h.say("alice", "!cc pack internal blackjack stand on")
    assert reply == "stand isn't in blackjack"
    usage = await h.say("alice", "!cc pack internal blackjack deal")
    assert usage is not None and "on|off" in usage


# ── system packs ─────────────────────────────────────────────────────────────
async def test_core_resolves_everywhere_without_being_published(h: Harness) -> None:  # noqa: F811
    await _install_core(h)

    assert await h.say("bob", "!echo x && false || default fell back") == "fell back"
    assert (await h.run("bob", "!false", OTHER_CHANNEL)).result.code == Code.FAIL
    assert [k for _, k in await h.packs.publications_in(GLOBAL) if k.name == CORE] == []


async def test_a_system_pack_cannot_be_published_or_changed_from_chat(h: Harness) -> None:  # noqa: F811
    await _install_core(h)
    core = await h.packs.system_pack(CORE)
    assert core is not None and core.system_version == CORE_VERSION

    with pytest.raises(SystemPackError):
        await h.packs.publish(channel_id=GLOBAL, pack=core, published_by=OWNER["id"])
    await h.say("owner", "!cc add hug echo hugs")
    for line in ("!cc pack add core hug", "!cc pack rm core false", "!cc pack delete core"):
        assert await h.say("owner", line) == "core is a system pack; only the pack script changes it"
    assert await h.say("owner", "!cc pack info core") == "core: default, false — system pack"


async def test_system_members_win_over_channel_commands(h: Harness) -> None:  # noqa: F811
    await _install_core(h)
    await h.say("mod", "!cc add false echo not false")
    await h.say("mod", "!cc publish false")
    assert (await h.run("bob", "!false")).result.code == Code.FAIL


async def test_a_sentinel_does_not_count_toward_the_custom_command_depth(h: Harness) -> None:  # noqa: F811
    await _install_core(h)
    await h.say("alice", "!cc add third default deep enough")
    await h.say("alice", "!cc add second third")
    await h.say("alice", "!cc add first second")
    assert await h.say("alice", "!first") == "deep enough"


async def test_startup_needs_core_at_the_current_version(
    h: Harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Never signed in: the script has no account to install under yet, so the bot starts and says so.
    assert await require_core(h.packs) is False
    await h.dbs.bot.execute(
        "INSERT INTO oauth_tokens (identity, user_id, login, access_token, updated_at)"
        " VALUES ('bot', %s, %s, 'x', 0)",
        (OWNER["id"], OWNER["name"]),
    )
    with pytest.raises(CoreNotInstalled, match="not installed.*scripts/starter_pack.py"):
        await require_core(h.packs)
    await install(h.dbs.bot, owner_user_id=OWNER["id"], owner_login=OWNER["name"])
    assert await require_core(h.packs) is True

    monkeypatch.setattr(system, "CORE_VERSION", CORE_VERSION + 1)  # the code moved on, the install didn't
    with pytest.raises(CoreNotInstalled, match=f"at version {CORE_VERSION}, and this bot needs"):
        await require_core(h.packs)


async def test_installing_core_again_changes_nothing(h: Harness) -> None:  # noqa: F811
    first = await install(h.dbs.bot, owner_user_id=OWNER["id"], owner_login=OWNER["name"])
    assert f"create system pack {CORE}" in first and f"publish {CORE} globally" not in first
    assert await install(h.dbs.bot, owner_user_id=OWNER["id"], owner_login=OWNER["name"]) == []


# ── the sentinel body check ──────────────────────────────────────────────────
def test_core_bodies_call_only_sentinels() -> None:
    check_core()


@pytest.mark.parametrize(
    ("body", "refusal"),
    [
        ("random 1-6", "calls random, which isn't a sentinel"),
        ("nosuch", "calls nosuch, which isn't a sentinel"),
        ("@mine", "calls a personal alias, @mine"),
        ("echo {!random 1-6}", "calls random, which isn't a sentinel"),
    ],
)
def test_a_sentinel_body_refuses_what_a_channel_can_switch_off(body: str, refusal: str) -> None:
    node = CustomCommandService.parse_body(body, "!")
    with pytest.raises(NotASentinel, match=refusal):
        check_sentinel_body("x", node, builtin_registry(), [d.name for d in CORE_COMMANDS])


def test_a_sentinel_body_may_call_its_siblings_and_primitive_sentinels() -> None:
    node = CustomCommandService.parse_body("echo a && false || default b | true", "!")
    check_sentinel_body("x", node, builtin_registry(), [d.name for d in CORE_COMMANDS])
