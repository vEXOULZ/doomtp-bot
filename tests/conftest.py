"""Shared fixtures.

Tests run against a real Postgres, not a stand-in: ADR-0003 wanted the test engine to be the production
engine, and ADR-0014 kept that. Point `TEST_DATABASE_URL` at any server you like, or start the one the
compose file defines:

    docker compose --profile test up -d postgres-test

Some tests also need an external tool — pg_dump, the Twitch CLI. Without it they skip, saying what to
install; with `--require-tools`, which CI passes, they fail instead (see `require_tool`).
"""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import psycopg
import pytest
from vex_platform.jobs import JobRuntime, Registry

from doomtp_bot.audit.log import TABLE as AUDIT_TABLE
from doomtp_bot.history.backfill import BackfillService
from doomtp_bot.history.jobs import BackfillJobs, register
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


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-tools",
        action="store_true",
        help="fail, rather than skip, a test whose external tool (pg_dump, the Twitch CLI) is missing",
    )


@pytest.fixture(scope="session")
def require_tool(pytestconfig: pytest.Config) -> Callable[[bool, str], None]:
    """`require_tool(ok, advice)`: carry on if the tool is usable, else skip — or fail under --require-tools.

    A missing tool is a skip on a dev box, where the client tools are optional. In CI it has to be a
    failure: a skip there means the suite has quietly stopped testing whatever needed the tool and still
    gone green — which CI's first runs did, with a pg_dump one major too old (ADR-0014). CI asks for
    that with the flag rather than being detected, so a run anywhere can opt in and nothing hangs on one
    CI provider's environment variables.
    """
    required = bool(pytestconfig.getoption("--require-tools"))

    def check(ok: bool, advice: str) -> None:
        if ok:
            return
        if required:
            pytest.fail(f"{advice} (--require-tools makes this a failure rather than a skip)", pytrace=False)
        pytest.skip(advice)

    return check


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
        databases = await Databases.open(dsn, migrate=True)
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
    databases = await Databases.open(dsn, migrate=True)
    try:
        yield dsn, databases
    finally:
        await databases.close()
        admin = await psycopg.AsyncConnection.connect(ADMIN_URL, autocommit=True)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.fixture
def empty_database() -> Iterator[str]:
    """A database of this test's own with nothing in it, not even the schemas: for migration tests."""
    name = f"doomtp_empty_{os.getpid()}_{next(_CASE_IDS)}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_database(ADMIN_URL, name)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


MakeBackfillJobs = Callable[..., Awaitable[BackfillJobs]]


async def _empty_jobs(dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(
            "TRUNCATE jobs.job_runs, jobs.job_run_events, jobs.procrastinate_jobs, jobs.procrastinate_workers"
            " RESTART IDENTITY CASCADE"
        )
        # DELETE, not TRUNCATE: a test's own open transaction may hold the table (`dbs`). The rows the
        # runtime and /api/v2's refusals wrote on their own connections, so committed.
        await conn.execute(
            f"DELETE FROM {AUDIT_TABLE} WHERE job_run_id IS NOT NULL OR action IN ('request.denied', 'request.failed')"
        )


@pytest.fixture
async def make_backfill_jobs(database_url: str) -> AsyncIterator[MakeBackfillJobs]:
    """`await make_backfill_jobs(service, start=False)`: `BackfillJobs` over a job runtime of its own (ADR-0027).

    The runtime keeps its own pool, so what it writes is committed, unlike the `dbs` fixture's: the job
    tables, and the audit rows its actions wrote, are emptied before and after the test. Ids start at 1.
    `start` runs the worker; without it runs are only queued.
    """
    await _empty_jobs(database_url)
    made: list[JobRuntime] = []

    async def make(service: BackfillService, *, start: bool = False) -> BackfillJobs:
        registry = Registry()
        register(registry, service)
        runtime = JobRuntime(
            registry,
            database_url,
            audit_table=AUDIT_TABLE,
            concurrency=1,
            max_concurrency=1,
            poll_interval=0.1,
            shutdown_timeout=2.0,
        )
        await runtime.open()
        made.append(runtime)
        if start:
            await runtime.start()
        return BackfillJobs(service, runtime)

    try:
        yield make
    finally:
        for runtime in made:
            await runtime.close()
        await _empty_jobs(database_url)
