"""Rewrite every stored body from syntax 1.0 to 2.0 (ADR-0018 item 7).

Custom command versions, triggers and callbacks keep the syntax version they were written in. This
rewrites each 1.0 row with `lang.migrate.migrate_v1`, checks the result parses, and marks it 2.0, all in
one transaction. A row whose rewrite doesn't parse is left at 1.0 and named, and nothing is written.

    python scripts/migrate_v2.py --database-url postgresql://doomtp@localhost/doomtp --dry-run
    python scripts/migrate_v2.py --database-url postgresql://doomtp@localhost/doomtp

Run it once, after deploying the 2.0 bot and before channels use it: a 1.0 body fails to parse in 2.0.
Running it again finds nothing to do, because rewritten rows are 2.0. It also lists what a person has to
look at, since no rewrite can decide it:

- bodies that read a result's `.code`: a missing placeholder is now `E_MISSING_VALUE` (230), not 2;
- custom commands named like a new built-in (`check`, `calc`, `ifelse`, the operator commands), which
  the built-in now shadows, or with a purely numeric name, which a bare expression line now takes.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

import psycopg

from doomtp_bot.config import Settings
from doomtp_bot.lang import SYNTAX_VERSION
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.migrate import migrate_v1
from doomtp_bot.lang.parser import Context, ParserParams, parse
from doomtp_bot.runtime import ops
from doomtp_bot.storage.db import Connection, configure_event_loop, connect, fetch_all, transaction

#: Built-in names that 2.0 adds, and so shadow a custom command of the same name.
NEW_BUILTINS = frozenset(
    {"check", "calc", "ifelse", "neg", "not", "and", "or", *ops.BINARY_COMMANDS, *ops.COMPARE_COMMANDS}
)
_READS_CODE = re.compile(r"\.code\b")

#: (label, select, update). Each select gives `ref` (for the report), `body`, and the key columns the
#: update takes after the new body.
TABLES: tuple[tuple[str, str, str], ...] = (
    (
        "custom command",
        "SELECT v.command_id, v.version, c.owner_login || '/' || c.name || ' v' || v.version AS ref,"
        " v.body FROM custom_command_versions v JOIN custom_commands c ON c.id = v.command_id"
        " WHERE v.syntax_version <> %s ORDER BY v.command_id, v.version",
        "UPDATE custom_command_versions SET body = %s, syntax_version = %s WHERE command_id = %s AND version = %s",
    ),
    (
        "trigger",
        "SELECT id, channel_id || ' #' || id || ' (' || type || ')' AS ref, expr AS body FROM triggers"
        " WHERE syntax_version <> %s ORDER BY id",
        "UPDATE triggers SET expr = %s, syntax_version = %s WHERE id = %s",
    ),
    (
        "callback",
        "SELECT channel_id, scope, kind, channel_id || ' ' || scope || ' ' || kind AS ref, expr AS body"
        " FROM callbacks WHERE syntax_version <> %s ORDER BY channel_id, scope, kind",
        "UPDATE callbacks SET expr = %s, syntax_version = %s WHERE channel_id = %s AND scope = %s AND kind = %s",
    ),
)


@dataclass(slots=True)
class Report:
    rewritten: dict[str, int] = field(default_factory=dict)
    #: (label, ref, 1.0 body, 2.0 body) for every row whose text changed.
    changes: list[tuple[str, str, str, str]] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    reads_code: list[str] = field(default_factory=list)
    shadowed: list[str] = field(default_factory=list)


async def migrate(conn: Connection, *, dry_run: bool = False) -> Report:
    """Rewrite every row below 2.0. Writes nothing if `dry_run` or if any rewrite fails to parse."""
    report = Report()
    updates: list[tuple[str, tuple[object, ...]]] = []
    for label, select, update in TABLES:
        rows = await fetch_all(conn, select, (SYNTAX_VERSION,))
        report.rewritten[label] = len(rows)
        for row in rows:
            old = row["body"]
            new = migrate_v1(old)
            try:
                parse(new, Context.BODY, ParserParams())
            except ParseError as exc:
                report.broken.append(f"{label} {row['ref']}: {exc}")
                continue
            if new != old:
                report.changes.append((label, row["ref"], old, new))
            if _READS_CODE.search(new):
                report.reads_code.append(f"{label} {row['ref']}: {new}")
            keys = tuple(v for k, v in row.items() if k not in ("ref", "body"))
            updates.append((update, (new, SYNTAX_VERSION, *keys)))

    for row in await fetch_all(
        conn,
        "SELECT owner_login, name FROM custom_commands WHERE status = 'active' ORDER BY owner_login, name",
    ):
        name = row["name"]
        if name in NEW_BUILTINS or name.isdigit():
            why = "a bare expression line takes a number" if name.isdigit() else "the built-in shadows it"
            report.shadowed.append(f"{row['owner_login']}/{name}: {why}")

    if dry_run or report.broken:
        return report
    async with transaction(conn):
        for sql, params in updates:
            await conn.execute(sql, params)
    return report


async def run(args: argparse.Namespace) -> int:
    try:
        conn = await connect(args.database_url or Settings().database_dsn(), "bot")
    except psycopg.OperationalError as exc:
        print(f"cannot reach the database: {exc}", file=sys.stderr)
        return 2
    try:
        report = await migrate(conn, dry_run=args.dry_run)
    finally:
        await conn.close()

    for label, ref, old, new in report.changes:
        print(f"{label} {ref}:\n  1.0  {old}\n  2.0  {new}")
    counts = ", ".join(f"{n} {label}s" for label, n in report.rewritten.items())
    for title, lines in (
        ("reads a result's .code, check it against the new codes", report.reads_code),
        ("named like a 2.0 built-in", report.shadowed),
        ("does not parse after the rewrite", report.broken),
    ):
        if lines:
            print(f"\n{title}:")
            for line in lines:
                print(f"  {line}")
    if report.broken:
        print(f"\nstopped: nothing written ({counts} below {SYNTAX_VERSION})", file=sys.stderr)
        return 1
    print(f"\n{'would rewrite' if args.dry_run else 'rewrote'} {counts} to {SYNTAX_VERSION}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--database-url", default=None, help="Postgres URL (default: the bot's own DATABASE_URL)"
    )
    parser.add_argument("--dry-run", action="store_true", help="show every rewrite, change nothing")
    args = parser.parse_args(argv)
    configure_event_loop()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
