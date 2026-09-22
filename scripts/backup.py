"""Consistent Postgres backups with rotation (ADR-0014).

`pg_dump` takes its snapshot inside a single transaction, so it is safe to run while the bot is
writing. Run it from host cron or as a compose one-shot:

    python scripts/backup.py --keep 7
    docker compose --profile tools run --rm backup

Each schema is dumped on its own — `bot` holds the state you cannot lose and `chatlog` holds the log
that grows without bound, and ADR-0003 split them precisely so their backup and retention need not
be the same decision. Each run writes `<schema>-<UTC timestamp>.dump` into `<out-dir>` and deletes
the oldest copies of that schema beyond `--keep`. Restore one with `pg_restore`.

The dumps land on the same host the database runs on, which is not a backup until a copy leaves the
machine. Getting them off the box is still the operator's job (README, "Deploying to a server").
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from doomtp_bot.config import Settings

SCHEMAS = ("bot", "chatlog")
KEEP_DEFAULT = 7
SUFFIX = ".dump"


class BackupError(RuntimeError):
    """pg_dump refused, or is not installed."""


def timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def backup_schema(dsn: str, schema: str, out_dir: Path, *, stamp: str | None = None) -> Path:
    """Dump one schema into `out_dir`. Returns the file written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{schema}-{stamp or timestamp()}{SUFFIX}"
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise BackupError("pg_dump is not installed")
    # --format=custom so pg_restore can pick pieces out of it, and compresses on the way; a partial
    # file from a failed run is removed rather than left looking like a backup.
    result = subprocess.run(
        [pg_dump, "--dbname", dsn, "--schema", schema, "--format", "custom",
         "--compress", "6", "--file", str(target)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )  # fmt: skip
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise BackupError(result.stderr.strip() or f"pg_dump exited {result.returncode}")
    return target


def rotate(out_dir: Path, schema: str, keep: int) -> list[Path]:
    """Delete all but the newest `keep` backups of one schema. Returns what was removed."""
    existing = sorted(out_dir.glob(f"{schema}-*{SUFFIX}"))  # timestamps sort chronologically
    stale = existing[: max(0, len(existing) - keep)] if keep >= 0 else []
    for path in stale:
        path.unlink()
    return stale


def run(dsn: str, out_dir: Path, keep: int, schemas: Sequence[str] = SCHEMAS) -> int:
    failures = 0
    for schema in schemas:
        try:
            written = backup_schema(dsn, schema, out_dir)
        except BackupError as exc:
            failures += 1
            print(f"failed {schema}: {exc}", file=sys.stderr)
            continue
        removed = rotate(out_dir, schema, keep)
        size_mb = written.stat().st_size / 1_048_576
        print(f"{written} ({size_mb:.1f} MiB){f', removed {len(removed)} old' if removed else ''}")
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--database-url", default=None, help="Postgres URL (default: the bot's own DATABASE_URL)"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=Path("/data/backups"), help="where the dumps are written"
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=KEEP_DEFAULT,
        help=f"backups to keep per schema (default {KEEP_DEFAULT})",
    )
    args = parser.parse_args(argv)
    dsn = args.database_url or Settings().database_dsn()
    return run(dsn, args.out_dir, args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
