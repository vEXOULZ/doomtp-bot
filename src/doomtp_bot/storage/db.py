"""SQLite connections with WAL and a numbered-SQL migrations runner (ADR-0003).

Migrations live in `migrations/<db>/NNNN_name.sql`. The applied version is tracked with
`PRAGMA user_version`; each file runs in its own transaction together with the version bump.
"""

from __future__ import annotations

import asyncio
import os
import re
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import aiosqlite
import structlog

log = structlog.get_logger(__name__)

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(db_name: str) -> list[Migration]:
    """Read migrations shipped in the package for `db_name` ('bot' or 'chatlog'), sorted by version."""
    root = resources.files("doomtp_bot.storage") / "migrations" / db_name
    migrations: list[Migration] = []
    for entry in root.iterdir():
        match = _MIGRATION_RE.match(entry.name)
        if not match:
            continue
        migrations.append(Migration(int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    migrations.sort(key=lambda m: m.version)
    expected = list(range(1, len(migrations) + 1))
    if [m.version for m in migrations] != expected:
        raise RuntimeError(f"{db_name} migrations must be numbered contiguously from 0001")
    return migrations


async def connect(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.commit()
    if os.name == "posix":
        os.chmod(path, 0o600)  # bot.db holds refresh tokens
    return conn


_WRITE_LOCKS: weakref.WeakKeyDictionary[aiosqlite.Connection, asyncio.Lock] = weakref.WeakKeyDictionary()


def write_lock(conn: aiosqlite.Connection) -> asyncio.Lock:
    """One lock per connection: every coroutine shares it, so transactions must not interleave."""
    lock = _WRITE_LOCKS.get(conn)
    if lock is None:
        lock = _WRITE_LOCKS[conn] = asyncio.Lock()
    return lock


@asynccontextmanager
async def transaction(
    conn: aiosqlite.Connection, *, immediate: bool = False
) -> AsyncIterator[aiosqlite.Connection]:
    """Serialized write transaction: commit on success, roll back on any exception.

    `immediate` takes SQLite's write lock up front. Don't nest these on the same connection.
    """
    async with write_lock(conn):
        if immediate:
            await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            await conn.rollback()
            raise
        else:
            await conn.commit()


async def current_version(conn: aiosqlite.Connection) -> int:
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def migrate(conn: aiosqlite.Connection, db_name: str) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    migrations = load_migrations(db_name)
    version = await current_version(conn)
    if version > len(migrations):
        raise RuntimeError(
            f"{db_name} schema version {version} is newer than this build ({len(migrations)}); refusing to start"
        )
    for migration in migrations[version:]:
        log.info("db.migrate", db=db_name, migration=migration.name)
        # executescript() commits any open transaction first; wrap explicitly so file + version bump are atomic.
        await conn.executescript(
            f"BEGIN;\n{migration.sql}\nPRAGMA user_version = {migration.version};\nCOMMIT;"
        )
    return await current_version(conn)


@dataclass
class Databases:
    bot: aiosqlite.Connection
    chatlog: aiosqlite.Connection

    @classmethod
    async def open(cls, bot_path: Path, chatlog_path: Path) -> Databases:
        bot = await connect(bot_path)
        chatlog = await connect(chatlog_path)
        await migrate(bot, "bot")
        await migrate(chatlog, "chatlog")
        return cls(bot=bot, chatlog=chatlog)

    async def close(self) -> None:
        await self.bot.close()
        await self.chatlog.close()
