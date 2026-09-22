"""Nightly backup script: consistent dumps plus rotation (ADR-0014)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from doomtp_bot.storage.db import Databases
from scripts.backup import BackupError, backup_schema, rotate, run

# pg_dump ships with the Postgres client tools, which a dev box need not have; the image the backup
# service runs from does (Dockerfile). Same rule as the Twitch CLI tests: skip rather than pretend.
needs_pg_dump = pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump is not installed")


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
