"""The starter set of derived commands: installed once, published globally, runnable (ADR-0012)."""

from __future__ import annotations

import argparse

import pytest

from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.storage.db import Databases, fetch_value
from scripts.starter_pack import PACK, STARTER, install, run
from tests.customcmds.test_customcmds import USERS
from tests.customcmds.test_packs import Harness, h  # noqa: F401

OWNER = USERS["owner"]


async def _install(harness: Harness, **kwargs: object) -> list[str]:
    return await install(harness.dbs.bot, owner_user_id=OWNER["id"], owner_login=OWNER["name"], **kwargs)  # type: ignore[arg-type]


async def test_the_starter_commands_run_in_a_channel_that_never_published_them(h: Harness) -> None:  # noqa: F811
    done = await _install(h)

    assert f"create pack {PACK}" in done and f"publish {PACK} globally" in done
    assert await h.say("alice", "!hug bob") == "Alice hugs bob 🫂"
    assert await h.say("alice", "!hug") == "Alice hugs the whole chat 🫂"
    assert (await h.say("alice", "!lurk") or "").startswith("thanks for the lurk, Alice")
    rolled = await h.say("alice", "!roll 1-6")
    assert rolled is not None and rolled.startswith("Alice rolled ")
    assert 1 <= int(rolled.rsplit(" ", 1)[1]) <= 6
    assert await h.say("alice", "!so bob") == "go follow twitch.tv/bob — they were last seen being excellent"


async def test_the_counter_waits_for_the_channel_to_allow_its_write(h: Harness) -> None:  # noqa: F811
    await _install(h)

    # It isn't the invoker's rank that writes the variable, it's the command: a mod is refused too.
    assert await h.say("alice", "!deaths") == "you can't change channel.deaths"
    assert await h.say("mod", "!deaths") == "you can't change channel.deaths"

    granted = await h.say("mod", "!cc grant deaths channel.deaths")  # a derived command is granted here
    assert granted is not None and granted.startswith("deaths can now write channel.deaths")
    assert await h.say("alice", "!deaths") == "deaths: 1"
    assert await h.say("bob", "!deaths") == "deaths: 2"


async def test_installing_again_changes_nothing(h: Harness) -> None:  # noqa: F811
    await _install(h)
    assert await _install(h) == []

    changed = await h.service.by_owner(OWNER["id"], "lurk")
    assert changed is not None
    await h.service.edit(changed, "echo drifted", channel_id=GLOBAL, prefix="!")

    assert await _install(h, dry_run=True) == ["edit lurk"]
    assert await h.say("alice", "!lurk") == "drifted"  # the dry run really didn't touch it
    assert await _install(h) == ["edit lurk"]
    assert (await h.say("alice", "!lurk") or "").startswith("thanks for the lurk")


async def test_every_starter_body_is_documented_and_parses(h: Harness) -> None:  # noqa: F811
    await _install(h)

    for derived in STARTER:
        command = await h.service.by_owner(OWNER["id"], derived.name)
        assert command is not None, derived.name
        assert command.summary == derived.summary
        h.service.parse_body(command.body, "!")  # raises if the body stopped parsing

    packs = await h.service.publications_in(GLOBAL)
    assert packs == []  # the commands arrive through the pack, not one publication each


def _args(dsn: str, **kwargs: object) -> argparse.Namespace:
    defaults: dict[str, object] = {"owner_id": "", "owner_login": "", "dry_run": False}
    return argparse.Namespace(database_url=dsn, **(defaults | kwargs))


async def test_the_script_installs_into_the_database_it_is_pointed_at(
    committed_database: tuple[str, Databases], capsys: pytest.CaptureFixture[str]
) -> None:
    """The command-line path, which opens its own connection: `install` alone can't show it still works."""
    dsn, dbs = committed_database
    assert await run(_args(dsn, owner_id=OWNER["id"], owner_login=OWNER["name"])) == 0
    assert f"publish {PACK} globally" in capsys.readouterr().out
    count = await fetch_value(
        dbs.bot, "SELECT count(*) FROM custom_commands WHERE owner_user_id = %s", (OWNER["id"],)
    )
    assert count == len(STARTER)


async def test_the_script_needs_an_owner_before_it_touches_anything(
    committed_database: tuple[str, Databases], capsys: pytest.CaptureFixture[str]
) -> None:
    dsn, _ = committed_database
    assert await run(_args(dsn)) == 2  # no bot account signed in, and none named
    assert "no bot account" in capsys.readouterr().err
