"""Consistent SQLite backups with rotation (ADR-0003).

Uses SQLite's online backup API, so it is safe to run while the bot is writing: the copy is a
transactionally consistent snapshot, WAL and all. Run it from host cron or as a compose one-shot:

    python scripts/backup.py --data-dir /data --keep 7
    docker compose run --rm backup

Each run writes `<name>-<UTC timestamp>.db.gz` into `<data-dir>/backups` and deletes the oldest
copies of that database beyond `--keep`.
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

DATABASES = ("bot.db", "chatlog.db")
KEEP_DEFAULT = 7
SUFFIX = ".db.gz"


def timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def backup_database(source: Path, out_dir: Path, *, stamp: str | None = None) -> Path:
    """Snapshot `source` into `out_dir`, gzipped. Returns the file written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{source.stem}-{stamp or timestamp()}{SUFFIX}"
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        raw = Path(tmp) / source.name
        # closing(), not `with sqlite3.connect(...)`: that commits a transaction but leaves the file open,
        # and Windows refuses to delete an open file.
        with (
            closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src,
            closing(sqlite3.connect(raw)) as dst,
        ):
            src.backup(dst)  # online backup API: consistent snapshot, no writer lock held
        with raw.open("rb") as plain, gzip.open(target, "wb") as compressed:
            shutil.copyfileobj(plain, compressed)
    return target


def rotate(out_dir: Path, name: str, keep: int) -> list[Path]:
    """Delete all but the newest `keep` backups of one database. Returns what was removed."""
    existing = sorted(out_dir.glob(f"{name}-*{SUFFIX}"))  # timestamps sort chronologically
    stale = existing[: max(0, len(existing) - keep)] if keep >= 0 else []
    for path in stale:
        path.unlink()
    return stale


def run(data_dir: Path, keep: int, databases: Sequence[str] = DATABASES) -> int:
    out_dir = data_dir / "backups"
    failures = 0
    for name in databases:
        source = data_dir / name
        if not source.is_file():
            print(f"skipped {source}: not found", file=sys.stderr)
            continue
        try:
            written = backup_database(source, out_dir)
        except sqlite3.Error as exc:
            failures += 1
            print(f"failed {source}: {exc}", file=sys.stderr)
            continue
        removed = rotate(out_dir, source.stem, keep)
        size_mb = written.stat().st_size / 1_048_576
        print(f"{written} ({size_mb:.1f} MiB){f', removed {len(removed)} old' if removed else ''}")
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path("/data"), help="where bot.db and chatlog.db live"
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=KEEP_DEFAULT,
        help=f"backups to keep per database (default {KEEP_DEFAULT})",
    )
    args = parser.parse_args(argv)
    return run(args.data_dir, args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
