"""Postgres connections and a numbered-SQL migrations runner (ADR-0014).

One database, two schemas: `bot` holds state and configuration, `chatlog` holds the message log. Each
gets its own connection whose `search_path` points at it, so queries name tables unqualified exactly as
they did when these were two SQLite files, and nothing can join across the two by accident.

Migrations live in `migrations/<schema>/NNNN_name.sql` and are recorded a row at a time in that schema's
`schema_migrations` table. Postgres has transactional DDL, so a migration and its version row either both
land or neither does — no half-applied schema to unpick by hand.

Connections are `autocommit=True`: a read should not leave a transaction open. Writes take an explicit
`transaction()` block, which serializes on a per-connection lock because every writer in the process
shares one connection. A pool belongs with the second process (ADR-0014), not here.
"""

from __future__ import annotations

import asyncio
import re
import sys
import weakref
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

log = structlog.get_logger(__name__)

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

Connection = psycopg.AsyncConnection[dict[str, Any]]
Row = dict[str, Any]
Params = Sequence[Any]


def configure_event_loop() -> None:
    """Pick an event loop psycopg can use. Call before `asyncio.run`, on Windows only in practice.

    Python defaults to the Proactor loop on Windows and psycopg's async mode refuses to run on it. The
    Selector loop it wants cannot spawn asyncio subprocesses — production is Linux, where none of this
    applies, but a dev box that needs both would have to run the database work in its own loop.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(schema: str) -> list[Migration]:
    """Read migrations shipped in the package for `schema` ('bot' or 'chatlog'), sorted by version."""
    root = resources.files("doomtp_bot.storage") / "migrations" / schema
    migrations: list[Migration] = []
    for entry in root.iterdir():
        match = _MIGRATION_RE.match(entry.name)
        if not match:
            continue
        migrations.append(Migration(int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    migrations.sort(key=lambda m: m.version)
    expected = list(range(1, len(migrations) + 1))
    if [m.version for m in migrations] != expected:
        raise RuntimeError(f"{schema} migrations must be numbered contiguously from 0001")
    return migrations


async def connect(dsn: str, schema: str) -> Connection:
    """Open a connection pinned to `schema`, creating the schema if this is a fresh database."""
    if not _SCHEMA_RE.match(schema):
        # Interpolated into SQL below: it comes from our own package layout, never from input, and this
        # keeps it that way.
        raise ValueError(f"not a usable schema name: {schema!r}")
    conn: Connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True, row_factory=dict_row)
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}", public')
    return conn


_WRITE_LOCKS: weakref.WeakKeyDictionary[Connection, asyncio.Lock] = weakref.WeakKeyDictionary()


def write_lock(conn: Connection) -> asyncio.Lock:
    """One lock per connection: every coroutine shares it, so transactions must not interleave."""
    lock = _WRITE_LOCKS.get(conn)
    if lock is None:
        lock = _WRITE_LOCKS[conn] = asyncio.Lock()
    return lock


@asynccontextmanager
async def transaction(conn: Connection) -> AsyncIterator[Connection]:
    """Serialized write transaction: commit on success, roll back on any exception.

    Don't nest these on the same connection — the lock is not reentrant.
    """
    async with write_lock(conn), conn.transaction():
        yield conn


async def fetch_all(conn: Connection, sql: str, params: Params = ()) -> list[Row]:
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(conn: Connection, sql: str, params: Params = ()) -> Row | None:
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()


async def fetch_value(conn: Connection, sql: str, params: Params = ()) -> Any:
    """First column of the first row, or None. For `SELECT count(*)`-shaped queries."""
    row = await fetch_one(conn, sql, params)
    return None if row is None else next(iter(row.values()))


async def execute(conn: Connection, sql: str, params: Params = ()) -> int:
    """Run a statement and return the number of rows it touched."""
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return cur.rowcount


async def current_version(conn: Connection) -> int:
    """The schema version this connection's schema is at. `migrate` has made sure the table exists."""
    value = await fetch_value(conn, "SELECT coalesce(max(version), 0) AS v FROM schema_migrations")
    return int(value or 0)


async def migrate(conn: Connection, schema: str) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    migrations = load_migrations(schema)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version integer PRIMARY KEY,"
        " name text NOT NULL,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    )
    version = await current_version(conn)
    if version > len(migrations):
        raise RuntimeError(
            f"{schema} schema version {version} is newer than this build ({len(migrations)}); refusing to start"
        )
    for migration in migrations[version:]:
        log.info("db.migrate", schema=schema, migration=migration.name)
        # Transactional DDL: the file and its version row land together or not at all.
        async with transaction(conn):
            await conn.execute(migration.sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
    return await current_version(conn)


@dataclass
class Databases:
    bot: Connection
    chatlog: Connection

    @classmethod
    async def open(cls, dsn: str) -> Databases:
        bot = await connect(dsn, "bot")
        chatlog = await connect(dsn, "chatlog")
        await migrate(bot, "bot")
        await migrate(chatlog, "chatlog")
        return cls(bot=bot, chatlog=chatlog)

    async def close(self) -> None:
        await self.bot.close()
        await self.chatlog.close()
