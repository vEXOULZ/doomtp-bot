"""Shared fixtures.

Tests run against a real Postgres, not a stand-in: ADR-0003 wanted the test engine to be the production
engine, and ADR-0014 kept that. Point `TEST_DATABASE_URL` at any server you like, or start the one the
compose file defines:

    docker compose --profile test up -d postgres-test
"""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest

from doomtp_bot.storage.db import Databases, configure_event_loop

# Before pytest-asyncio builds a loop: on Windows the default one is a loop psycopg refuses to use.
configure_event_loop()

ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:55432/postgres")
START_HINT = (
    f"cannot reach Postgres at {ADMIN_URL.rsplit('@', 1)[-1]}.\n"
    "Start it with:  docker compose --profile test up -d postgres-test\n"
    "or point TEST_DATABASE_URL at your own server."
)


_CASE_IDS = itertools.count()


def _with_database(url: str, name: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{name}"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A database of this session's own, migrated once and dropped at the end."""
    name = f"doomtp_test_{os.getpid()}"
    dsn = _with_database(ADMIN_URL, name)

    async def create() -> None:
        try:
            conn = await psycopg.AsyncConnection.connect(ADMIN_URL, autocommit=True)
        except psycopg.OperationalError as exc:
            pytest.exit(f"{START_HINT}\n\n{exc}", returncode=1)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()
        databases = await Databases.open(dsn)
        await databases.close()

    async def drop() -> None:
        conn = await psycopg.AsyncConnection.connect(ADMIN_URL, autocommit=True)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()

    asyncio.run(create())
    try:
        yield dsn
    finally:
        asyncio.run(drop())


@pytest.fixture
async def dbs(database_url: str) -> AsyncIterator[Databases]:
    """Migrated `bot` and `chatlog` schemas, undone again when the test ends.

    Each connection runs inside a transaction that is always rolled back, so a test sees a clean schema
    without anyone having to re-truncate and re-seed it — the migration stays the only place that says
    what an empty database contains. Writes the code makes through `storage.db.transaction()` nest as
    savepoints inside it and behave normally.
    """
    databases = await Databases.open(database_url)
    try:
        async with databases.bot.transaction() as bot_tx, databases.chatlog.transaction():
            yield databases
            # Only on a passing test. A failing one raises through both blocks instead, which rolls them
            # back just the same — raising this from a `finally` would bury the test's own exception.
            raise psycopg.Rollback(bot_tx)
    finally:
        await databases.close()


@pytest.fixture
async def committed_database(database_url: str) -> AsyncIterator[tuple[str, Databases]]:
    """A database of this test's own, with writes actually committed.

    The `dbs` fixture isolates by never committing, which is invisible to anything that opens its own
    connection — `scripts/coverage.py` and `scripts/backup.py` both do. Those tests get a real database
    instead, and pay a create-and-drop for it.
    """
    name = f"doomtp_case_{os.getpid()}_{next(_CASE_IDS)}"
    dsn = _with_database(ADMIN_URL, name)
    admin = await psycopg.AsyncConnection.connect(ADMIN_URL, autocommit=True)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    databases = await Databases.open(dsn)
    try:
        yield dsn, databases
    finally:
        await databases.close()
        admin = await psycopg.AsyncConnection.connect(ADMIN_URL, autocommit=True)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()
