"""The derived commands the bot ships: the `core` system pack and the `starter` set (ADR-0012 item 6,
ADR-0019).

A derived command is an ordinary custom command, so these are written in the command language rather than
Python: readable with `!cc info`, versioned, fixable live. They belong to the bot's own account and go
out as packs:

- `core` is a **system pack**: sentinels like `false` and `default`, which resolve everywhere without being
  published and can't be switched off. The bot refuses to start without it, so run this script before the
  first start and after every upgrade.
- `starter` is published globally, and a channel switches it off like anything else
  (`!module disable starter`, or `!cmd disable hug`).

    python scripts/starter_pack.py --database-url postgresql://doomtp@localhost/doomtp
    docker compose --profile tools run --rm starter-pack

Running it again edits what changed here and leaves the rest alone, which is how an upgrade ships a fix.
The starter pack is safe to update while the bot is running: resolution reads publications from the
database every time, and an edit bumps the version the parsed-body cache is keyed by. The bot loads
`core` once at startup, so restart it after a `core` change.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

import psycopg

from doomtp_bot.config import Settings
from doomtp_bot.customcmds.packs import RESERVED_PACK_NAMES, Pack, PackService
from doomtp_bot.customcmds.service import CustomCommandError, CustomCommandService
from doomtp_bot.customcmds.system import (
    CORE,
    CORE_COMMANDS,
    CORE_SUMMARY,
    CORE_VERSION,
    Derived,
    NotASentinel,
    check_sentinel_body,
)
from doomtp_bot.filters.service import FilterService
from doomtp_bot.lang.parser import DEFAULT_PREFIX
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.storage.db import Connection, check_schema, configure_event_loop, connect
from doomtp_bot.storage.schema import SchemaMismatch

PACK = "starter"
PACK_SUMMARY = "The commands every channel starts with"


STARTER: tuple[Derived, ...] = (
    Derived(
        name="hug",
        summary="Hug someone, or the whole chat",
        body="echo {$chatter.display} hugs {arg.1 ?? the whole chat} 🫂",
        declarations=('1 name=target type=user required=no "who to hug"',),
    ),
    Derived(
        name="lurk",
        summary="Say you're still around, quietly",
        body="echo thanks for the lurk, {$chatter.display} — your seat stays warm",
    ),
    Derived(
        name="roll",
        summary="Roll a die",
        body="random {arg.1 ?? 1-20} | echo {$chatter.display} rolled {_1}",
        declarations=('1 name=range type=range required=no "a range like 1-6 (default 1-20)"',),
    ),
    Derived(
        name="so",
        summary="Shout out another streamer",
        # A moderator's `so` also sends Twitch's shoutout card, when the bot is a moderator and the stream
        # is live; for anyone else, or when the card can't go out, it is just the line.
        body=(
            "ifelse {$chatter.is_mod} ( shoutout {arg.streamer[name]} || true )"
            " && echo go follow twitch.tv/{arg.streamer[name]} — they were last seen being excellent"
        ),
        declarations=('1 name=streamer type=user "whose channel to name"',),
        note="anyone can run it; only a moderator's also sends Twitch's shoutout card",
    ),
    Derived(
        name="deaths",
        summary="Count the deaths of the run",
        body="var incr channel.deaths | echo deaths: {_1}",
        note="writes a channel variable, so each channel allows it once: `!cc grant deaths channel.deaths`",
    ),
    Derived(
        name="weather",
        summary="The weather somewhere right now",
        # wttr.in needs no key. `http` runs only in a bot admin's commands and only against hosts an admin
        # allowed (ADR-0020), so until both are true this fails with that reason and fetches nothing.
        body=(
            "http get https://wttr.in/{arg.place}?format=j1"
            " | echo {_1[nearest_area][0][areaName][0][value]}: {_1[current_condition][0][temp_C]}°C"
            " / {_1[current_condition][0][temp_F]}°F, {_1[current_condition][0][weatherDesc][0][value]}"
        ),
        declarations=('1+ name=place required=yes "a city or place, like Lisbon"',),
        note="needs the bot account to be a bot admin (`!admin add <bot>`) and `!admin http allow wttr.in`",
    ),
)


QUOTES_PACK = "quotes"
QUOTES_SUMMARY = "The channel's numbered quotes"
_Q, _N = "publisher.channel.quotes", "publisher.channel.quote_next"
_NO_ARG = '(arg.1 ?? "-")'  # `""` counts as missing (spec §7.3.3), so "no argument" compares as "-"

# A quote is a map in the bot's `publisher.channel.quotes`, keyed by its number: `text`, `date`, and `game`
# when the stream was live. `publisher.channel.quote_next` is the last number given out, so a deleted
# quote's number is never given again. Only this pack's commands write them (ADR-0019 item 9).
QUOTES: tuple[Derived, ...] = (
    Derived(
        name="quote",
        summary="Read, add and delete the channel's quotes",
        body=(
            f'ifelse {{{_NO_ARG} == "add"}} ( quote_add {{arg.2+}} )'
            f' ( ifelse {{{_NO_ARG} == "del" or {_NO_ARG} == "delete"}} ( quote_del {{arg.2}} )'
            f' ( ifelse {{{_NO_ARG} == "-"}} ( quote_random ) ( quote_show {{arg.1}} ) ) )'
        ),
        declarations=('1+ name=what required=no "a number, or add <text>, or del <number>"',),
    ),
    Derived(
        name="quote_add",
        summary="Add a quote (moderators)",
        body=(
            "ifelse {$chatter.is_mod}"
            " ( ifelse {arg.text:len > 400} ( fail 2 a quote is at most 400 characters )"
            f" ( var incr {_N} && var set {_Q}[{{{_N}}}][text] {{arg.text}}"
            f" && var set {_Q}[{{{_N}}}][date] {{$now.date}}"
            ' && ( ifelse {$channel.live and ($channel.game ?? "-") != "-"}'
            f" ( var set {_Q}[{{{_N}}}][game] {{$channel.game}} ) )"
            f" && echo added #{{{_N}}} ) )"
            " ( fail adding quotes takes moderator rank )"
        ),
        declarations=('1+ name=text required=yes "the quote"',),
        internal=True,
    ),
    Derived(
        name="quote_del",
        summary="Delete a quote (moderators); its number stays taken",
        body=(
            "ifelse {$chatter.is_mod}"
            f" ( ifelse {{({_Q}[arg.number] ?? 0) != 0}}"
            f" ( var del {_Q}[{{arg.number}}] && echo deleted #{{arg.number}} )"
            " ( fail 3 there is no quote #{arg.number} ) )"
            " ( fail deleting quotes takes moderator rank )"
        ),
        declarations=('1 name=number type=int required=yes "which quote, by number"',),
        internal=True,
    ),
    Derived(
        name="quote_show",
        summary="Say one quote",
        body=(
            f"ifelse {{({_Q}[arg.number] ?? 0) != 0}}"
            f' ( ifelse {{"game" in {_Q}[arg.number]}}'
            f" ( echo #{{arg.number}}: {{{_Q}[arg.number][text]}} [{{{_Q}[arg.number][game]}}, {{{_Q}[arg.number][date]}}] )"
            f" ( echo #{{arg.number}}: {{{_Q}[arg.number][text]}} [{{{_Q}[arg.number][date]}}] ) )"
            " ( fail 3 there is no quote #{arg.number} )"
        ),
        declarations=('1 name=number type=int required=yes "which quote, by number"',),
        internal=True,
    ),
    Derived(
        name="quote_random",
        summary="Say a random quote",
        body=(
            f"ifelse {{({_Q}:len ?? 0) > 0}}"
            f" ( random 1-{{{_Q}:len}} | quote_show {{{_Q}:keys[_1 - 1]}} )"
            " ( fail 3 no quotes yet — {$channel.prefix}quote add <text> )"
        ),
        internal=True,
    ),
)


def check_core() -> None:
    """Every `core` body may call only what can never be switched off (ADR-0019). Raises NotASentinel."""
    registry = builtin_registry()
    names = [d.name for d in CORE_COMMANDS]
    for derived in CORE_COMMANDS:
        if registry.get(derived.name) is not None:
            raise NotASentinel(f"{derived.name} is a built-in already")
        body = CustomCommandService.parse_body(derived.body, DEFAULT_PREFIX)
        check_sentinel_body(derived.name, body, registry, names)


async def install(
    conn: Connection, *, owner_user_id: str, owner_login: str, dry_run: bool = False
) -> list[str]:
    """Create or update `core` and the starter commands, and publish the starter pack globally. Returns
    what it did. Raises NotASentinel before changing anything if a `core` body isn't a sentinel's."""
    check_core()
    filters = FilterService(conn)
    await filters.reload()  # the global list: these bodies are read out in every channel
    commands = CustomCommandService(conn, filters=filters)
    packs = PackService(conn, commands)
    owner = (owner_user_id, owner_login)
    done = await _install(
        commands, packs, owner, CORE, CORE_SUMMARY, CORE_COMMANDS, CORE_VERSION, dry_run=dry_run
    )
    done += await _install(commands, packs, owner, PACK, PACK_SUMMARY, STARTER, None, dry_run=dry_run)
    done += await _install(commands, packs, owner, QUOTES_PACK, QUOTES_SUMMARY, QUOTES, None, dry_run=dry_run)
    return done


async def _install(
    commands: CustomCommandService,
    packs: PackService,
    owner: tuple[str, str],
    name: str,
    summary: str,
    derived_commands: tuple[Derived, ...],
    system_version: int | None,
    *,
    dry_run: bool,
) -> list[str]:
    """One pack: a system pack when `system_version` is set, else published globally."""
    owner_user_id, owner_login = owner
    done: list[str] = []
    pack: Pack | None = await packs.by_owner(owner_user_id, name)
    if pack is None:
        done.append(f"create {'system ' if system_version else ''}pack {name}")
        if not dry_run:
            pack = await packs.create(
                owner_user_id=owner_user_id,
                name=name,
                summary=summary,
                actor_via="script",
                system_version=system_version,
                replaces_module=name in RESERVED_PACK_NAMES,
            )
    members = {c.name for c in await packs.members(pack.id)} if pack is not None else set()
    internal = await packs.internal_names(pack.id) if pack is not None else set()

    for derived in derived_commands:
        command = await commands.by_owner(owner_user_id, derived.name)
        declared = derived.params()
        if command is None:
            done.append(f"add {derived.name}")
        elif command.body != derived.body:
            done.append(f"edit {derived.name}")
        elif command.params != declared or command.summary != derived.summary:
            done.append(f"redescribe {derived.name}")
        if derived.name not in members:
            done.append(f"put {derived.name} in {name}")
        if derived.internal != (derived.name in internal):
            done.append(f"make {derived.name} {'internal' if derived.internal else 'public'}")
        if dry_run:
            continue

        if command is None:
            command = await commands.create(
                owner_user_id=owner_user_id,
                owner_login=owner_login,
                name=derived.name,
                body=derived.body,
                channel_id=GLOBAL,
                prefix=DEFAULT_PREFIX,
                actor_via="script",
            )
        elif command.body != derived.body:
            command = await commands.edit(
                command, derived.body, channel_id=GLOBAL, prefix=DEFAULT_PREFIX, actor_via="script"
            )
        if command.params != declared:
            command = await commands.set_params(command, list(declared), actor_via="script")
        if command.summary != derived.summary:
            await commands.set_summary(command, derived.summary, actor_via="script")
        if pack is not None and derived.name not in members:
            await packs.add_member(pack, command, actor_via="script")
        if pack is not None and derived.internal != (derived.name in internal):
            await packs.set_internal(pack, command, derived.internal, actor_via="script")

    if system_version is not None:  # it resolves everywhere unpublished; startup checks the version
        if pack is not None and (pack.system_version or 0) < system_version:
            done.append(f"mark {name} version {system_version}")
            if not dry_run:
                await packs.set_system_version(pack, system_version)
        return done
    if pack is None or not any(p.pack_id == pack.id for p, _ in await packs.publications_in(GLOBAL)):
        done.append(f"publish {name} globally")
    if pack is not None and not dry_run:
        await packs.publish(channel_id=GLOBAL, pack=pack, published_by=owner_user_id, actor_via="script")
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
        # The migrate step upgrades the schema just before this runs (ADR-0022). Run alone, this checks
        # it instead: the pack is written for this build's columns.
        try:
            await check_schema(conn, "bot")
        except SchemaMismatch as exc:
            print(f"stopped: {exc}", file=sys.stderr)
            return 1
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
        except (CustomCommandError, NotASentinel) as exc:
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
