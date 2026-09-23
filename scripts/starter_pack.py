"""The starter set of derived commands (ADR-0012 item 6).

A derived command is an ordinary custom command published to the global scope, so these are written in
the command language rather than Python: readable with `!cc info`, versioned, fixable live, and switched
off per channel like anything else (`!module disable starter`, or `!cmd disable hug`).

They belong to the bot's own account and go out as one pack, so a channel takes the set or none of it:

    python scripts/starter_pack.py --database-url postgresql://doomtp@localhost/doomtp
    docker compose --profile tools run --rm starter-pack

Running it again edits what changed here and leaves the rest alone, which is how an upgrade ships a fix.
It is safe while the bot is running: resolution reads publications from the database every time, and an
edit bumps the version the parsed-body cache is keyed by.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg

from doomtp_bot.config import Settings
from doomtp_bot.customcmds import params
from doomtp_bot.customcmds.packs import PackService
from doomtp_bot.customcmds.service import CustomCommandError, CustomCommandService
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.storage.db import Connection, configure_event_loop, connect

PACK = "starter"
PACK_SUMMARY = "The commands every channel starts with"


@dataclass(frozen=True, slots=True)
class Derived:
    """One starter command, exactly as `!cc add`, `!cc describe` and `!cc param` would leave it."""

    name: str
    summary: str
    body: str
    #: Parameter declarations in the syntax `!cc param <name>` takes, minus the command name.
    declarations: tuple[str, ...] = field(default_factory=tuple)
    #: Printed after the install when the command needs something from the channel before it works.
    note: str = ""

    def params(self) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = []
        for declaration in self.declarations:
            position, _, rest = declaration.partition(" ")
            assignments, description = params.split_declaration(rest)
            rows = params.declare(rows, position, assignments, description)
        return tuple(rows)


STARTER: tuple[Derived, ...] = (
    Derived(
        name="hug",
        summary="Hug someone, or the whole chat",
        body="echo {chatter.display} hugs {arg.1 ?? the whole chat} 🫂",
        declarations=('1 name=target type=user required=no "who to hug"',),
    ),
    Derived(
        name="lurk",
        summary="Say you're still around, quietly",
        body="echo thanks for the lurk, {chatter.display} — your seat stays warm",
    ),
    Derived(
        name="roll",
        summary="Roll a die",
        body="random {arg.1 ?? 1-20} | echo {chatter.display} rolled {1}",
        declarations=('1 name=range type=range required=no "a range like 1-6 (default 1-20)"',),
    ),
    Derived(
        name="so",
        summary="Shout out another streamer",
        body="echo go follow twitch.tv/{arg.1} — they were last seen being excellent",
        declarations=('1 name=streamer type=user "whose channel to name"',),
        note="anyone can run it; restrict it per channel with `!perm set so moderator`",
    ),
    Derived(
        name="deaths",
        summary="Count the deaths of the run",
        body="var incr channel.deaths | echo deaths: {1}",
        note="writes a channel variable, so each channel allows it once: `!cc grant deaths channel.deaths`",
    ),
)


async def install(
    conn: Connection, *, owner_user_id: str, owner_login: str, dry_run: bool = False
) -> list[str]:
    """Create or update the starter commands and publish the pack globally. Returns what it did."""
    commands = CustomCommandService(conn)
    packs = PackService(conn, commands)
    done: list[str] = []

    pack = await packs.by_owner(owner_user_id, PACK)
    if pack is None:
        done.append(f"create pack {PACK}")
        if not dry_run:
            pack = await packs.create(owner_user_id=owner_user_id, name=PACK, summary=PACK_SUMMARY)
    members = {c.name for c in await packs.members(pack.id)} if pack is not None else set()

    for derived in STARTER:
        command = await commands.by_owner(owner_user_id, derived.name)
        declared = derived.params()
        if command is None:
            done.append(f"add {derived.name}")
        elif command.body != derived.body:
            done.append(f"edit {derived.name}")
        elif command.params != declared or command.summary != derived.summary:
            done.append(f"redescribe {derived.name}")
        if derived.name not in members:
            done.append(f"put {derived.name} in {PACK}")
        if dry_run:
            continue

        if command is None:
            command = await commands.create(
                owner_user_id=owner_user_id,
                owner_login=owner_login,
                name=derived.name,
                body=derived.body,
                actor_via="script",
            )
        elif command.body != derived.body:
            command = await commands.edit(command, derived.body, actor_via="script")
        if command.params != declared:
            command = await commands.set_params(command, list(declared), actor_via="script")
        if command.summary != derived.summary:
            await commands.set_summary(command, derived.summary, actor_via="script")
        if pack is not None and derived.name not in members:
            await packs.add_member(pack, command)

    if pack is None or not any(p.pack_id == pack.id for p, _ in await packs.publications_in(GLOBAL)):
        done.append(f"publish {PACK} globally")
    if pack is not None and not dry_run:
        await packs.publish(channel_id=GLOBAL, pack=pack, published_by=owner_user_id)
    return done


async def bot_account(conn: Connection) -> tuple[str, str] | None:
    """The bot's own account, which is who these commands come from."""
    async with await conn.execute("SELECT user_id, login FROM oauth_tokens WHERE identity = 'bot'") as cur:
        row = await cur.fetchone()
    return (row["user_id"], row["login"]) if row else None


async def run(args: argparse.Namespace) -> int:
    try:
        conn = await connect(args.database_url or Settings().database_dsn(), "bot")
    except psycopg.OperationalError as exc:
        print(f"cannot reach the database: {exc}", file=sys.stderr)
        return 2
    try:
        owner = (args.owner_id, args.owner_login) if args.owner_id and args.owner_login else None
        owner = owner or await bot_account(conn)
        if owner is None:
            print(
                "no bot account in this database yet: sign the bot in first, or pass --owner-id"
                " and --owner-login",
                file=sys.stderr,
            )
            return 2
        try:
            done = await install(conn, owner_user_id=owner[0], owner_login=owner[1], dry_run=args.dry_run)
        except CustomCommandError as exc:
            print(f"stopped: {exc}", file=sys.stderr)
            return 1
    finally:
        await conn.close()

    changes = ", ".join(done) if done else "nothing to do"
    print(f"{'would: ' if args.dry_run else ''}{changes} (as @{owner[1]})")
    for derived in STARTER:
        if derived.note:
            print(f"  {derived.name}: {derived.note}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--database-url", default=None, help="Postgres URL (default: the bot's own DATABASE_URL)"
    )
    parser.add_argument("--owner-id", default="", help="Twitch user ID to own the commands")
    parser.add_argument("--owner-login", default="", help="that account's login")
    parser.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    args = parser.parse_args(argv)
    configure_event_loop()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
