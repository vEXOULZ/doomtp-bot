"""Nightly backup script: consistent snapshots plus rotation (ADR-0003)."""

from __future__ import annotations

import gzip
import sqlite3
from contextlib import closing
from pathlib import Path

from doomtp_bot.storage.db import Databases
from scripts.backup import backup_database, rotate, run


async def test_backup_snapshots_an_open_database(tmp_path: Path, dbs: Databases) -> None:
    await dbs.chatlog.execute(
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, sent_at, received_at)"
        " VALUES ('m1', 'c1', 'u1', 'alice', 'hello', 1, 1)"
    )
    await dbs.chatlog.commit()
    await dbs.chatlog.execute(  # uncommitted: must not appear in the snapshot
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, sent_at, received_at)"
        " VALUES ('m2', 'c1', 'u1', 'alice', 'not yet', 2, 2)"
    )

    written = backup_database(tmp_path / "chatlog.db", tmp_path / "backups")

    assert written.name.startswith("chatlog-") and written.suffixes[-2:] == [".db", ".gz"]
    restored = tmp_path / "restored.db"
    with gzip.open(written, "rb") as archive:
        restored.write_bytes(archive.read())
    with closing(sqlite3.connect(restored)) as conn:
        assert [r[0] for r in conn.execute("SELECT message_id FROM messages")] == ["m1"]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_rotation_keeps_the_newest(tmp_path: Path) -> None:
    out = tmp_path / "backups"
    out.mkdir()
    stamps = ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"]
    for stamp in stamps:
        (out / f"bot-{stamp}.db.gz").write_bytes(b"x")
    (out / "chatlog-20260101T000000Z.db.gz").write_bytes(b"x")  # other database: untouched

    removed = rotate(out, "bot", keep=2)

    assert [p.name for p in removed] == ["bot-20260101T000000Z.db.gz"]
    assert sorted(p.name for p in out.iterdir()) == [
        "bot-20260102T000000Z.db.gz",
        "bot-20260103T000000Z.db.gz",
        "chatlog-20260101T000000Z.db.gz",
    ]


async def test_run_backs_up_both_databases_and_skips_missing(tmp_path: Path, dbs: Databases) -> None:
    assert run(tmp_path, keep=7) == 0
    assert sorted(p.name.split("-")[0] for p in (tmp_path / "backups").iterdir()) == ["bot", "chatlog"]
    assert run(tmp_path / "empty", keep=7) == 0  # no databases there: nothing to do, no failure
