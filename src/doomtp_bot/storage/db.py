"""Postgres connections, and the schema check at startup (ADR-0014, ADR-0022).

One database, two schemas: `bot` holds state and configuration, `chatlog` holds the message log. Each
gets its own connection whose `search_path` points at it, so queries name tables unqualified exactly as
they did when these were two SQLite files, and nothing can join across the two by accident.

Migrations are Alembic revisions under `migrations/<schema>/` (`storage.schema`). The bot doesn't run
them: the `migrate` step does, before it starts, and `Databases.open` only checks that each schema is at
this build's head.

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
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from doomtp_bot.storage import schema as schema_module

log = structlog.get_logger(__name__)

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


async def schema_revision(conn: Connection) -> str | None:
    """The Alembic revision this connection's schema is at, or None if it has none."""
    if await fetch_value(conn, "SELECT to_regclass('alembic_version') AS t") is None:
        return None
    value = await fetch_value(conn, "SELECT version_num FROM alembic_version")
    return None if value is None else str(value)


async def check_schema(conn: Connection, schema: str) -> str:
    """Refuse a schema that isn't at this build's head, saying which way to fix it. Returns the revision."""
    expected = schema_module.head(schema)
    revision = await schema_revision(conn)
    if revision == expected:
        return revision
    if revision is None or schema_module.known(schema, revision):
        raise schema_module.SchemaMismatch(
            f"{schema} schema is at {revision or 'nothing'}, this build needs {expected}: "
            "run `doomtp-bot db upgrade` (the migrate step) first"
        )
    raise schema_module.SchemaMismatch(
        f"{schema} schema is at {revision}, which a newer build wrote; this build needs {expected}: "
        "downgrade with the newer image first (deploy/rollback.sh)"
    )


@dataclass
class Databases:
    bot: Connection
    chatlog: Connection

    @classmethod
    async def open(cls, dsn: str, *, migrate: bool = False) -> Databases:
        """Connect to both schemas and check them. `migrate` upgrades them first: tests and dev tools
        only, since a deployed bot leaves that to the migrate step (ADR-0022)."""
        if migrate:
            await asyncio.to_thread(schema_module.upgrade, dsn)
        bot = await connect(dsn, "bot")
        chatlog = await connect(dsn, "chatlog")
        try:
            await check_schema(bot, "bot")
            await check_schema(chatlog, "chatlog")
        except BaseException:
            await bot.close()
            await chatlog.close()
            raise
        return cls(bot=bot, chatlog=chatlog)

    async def close(self) -> None:
        await self.bot.close()
        await self.chatlog.close()
