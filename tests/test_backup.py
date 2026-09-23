"""Nightly backup script: consistent dumps plus rotation (ADR-0014)."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from doomtp_bot.storage.db import Databases
from scripts.backup import BackupError, backup_schema, rotate, run


def pg_major(version_text: str) -> int:
    """`pg_dump (PostgreSQL) 17.11 (Ubuntu 17.11-1.pgdg24.04+2)` -> 17."""
    match = re.search(r"\(PostgreSQL\)\s+(\d+)", version_text)
    if match is None:
        raise ValueError(f"not a PostgreSQL version line: {version_text!r}")
    return int(match.group(1))


@pytest.fixture(scope="session")
def pg_dump_ready(database_url: str, require_tool: Callable[[bool, str], None]) -> None:
    """pg_dump is installed and new enough to dump the test server, or the backup tests don't run.

    pg_dump refuses to dump a server newer than itself, so "installed" is not enough — a client one
    major behind fails every dump with an error that reads like a backup bug.

    A dev box need not have it — the image the backup service runs from has the right one (Dockerfile) —
    so there this skips; under --require-tools, as in CI, it fails (`require_tool` in conftest).
    """
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
    assert row is not None
    server = int(row[0]) // 10_000

    path = shutil.which("pg_dump")
    if path is None:
        problem = "pg_dump is not installed"
    else:
        version = subprocess.run([path, "--version"], capture_output=True, encoding="utf-8", check=True)
        client = pg_major(version.stdout)
        if client >= server:
            return
        problem = f"pg_dump {client} cannot dump a Postgres {server} server"

    require_tool(False, f"{problem}: install postgresql-client-{server} and put it first on PATH")


needs_pg_dump = pytest.mark.usefixtures("pg_dump_ready")


@pytest.mark.parametrize(
    ("line", "major"),
    [
        ("pg_dump (PostgreSQL) 17.11 (Ubuntu 17.11-1.pgdg24.04+2)", 17),
        ("pg_dump (PostgreSQL) 16.15 (Debian 16.15-0+deb13u1)", 16),
        ("pg_dump (PostgreSQL) 18beta1", 18),
    ],
)
def test_the_client_major_is_read_from_its_version_line(line: str, major: int) -> None:
    assert pg_major(line) == major


@needs_pg_dump
async def test_a_dump_can_be_restored(tmp_path: Path, committed_database: tuple[str, Databases]) -> None:
    dsn, dbs = committed_database
    await dbs.chatlog.execute(
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, sent_at, received_at)"
        " VALUES ('m1', 'c1', 'u1', 'alice', 'hello', 1, 1)"
    )

    written = backup_schema(dsn, "chatlog", tmp_path / "backups")

    assert written.name.startswith("chatlog-") and written.suffix == ".dump"
    listing = subprocess.run(  # noqa: ASYNC221 — backup.py is a sync script by design
        [shutil.which("pg_restore") or "pg_restore", "--list", str(written)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert listing.returncode == 0
    assert "messages" in listing.stdout  # the table is in there, and pg_restore can read the file


@needs_pg_dump
async def test_each_schema_is_dumped_separately(
    tmp_path: Path, committed_database: tuple[str, Databases]
) -> None:
    """ADR-0003 split state from the log so their retention could differ; that survives the port."""
    dsn, _ = committed_database
    assert run(dsn, tmp_path, keep=7) == 0
    dumped = sorted(p.name.split("-")[0] for p in tmp_path.iterdir())  # noqa: ASYNC240 — as above
    assert dumped == ["bot", "chatlog"]


def test_an_unreachable_database_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    dsn = "postgresql://nobody@127.0.0.1:1/nothing"
    if shutil.which("pg_dump") is None:
        with pytest.raises(BackupError, match="not installed"):
            backup_schema(dsn, "bot", tmp_path)
        return
    assert run(dsn, tmp_path, keep=7) == 2  # both schemas failed, and it said so rather than raising
    assert list(tmp_path.iterdir()) == []  # no half-written file left looking like a backup


def test_rotation_keeps_the_newest(tmp_path: Path) -> None:
    stamps = ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"]
    for stamp in stamps:
        (tmp_path / f"bot-{stamp}.dump").write_bytes(b"x")
    (tmp_path / "chatlog-20260101T000000Z.dump").write_bytes(b"x")  # other schema: untouched

    removed = rotate(tmp_path, "bot", keep=2)

    assert [p.name for p in removed] == ["bot-20260101T000000Z.dump"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "bot-20260102T000000Z.dump",
        "bot-20260103T000000Z.dump",
        "chatlog-20260101T000000Z.dump",
    ]
