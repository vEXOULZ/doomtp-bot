"""What the chat log covers, and whether the last restart left a hole (ADR-0008).

Run it after a deploy, when the bot is back up and backfill has had its pass:

    python scripts/coverage.py --database-url postgresql://doomtp@postgres/doomtp
    docker compose --profile tools run --rm coverage

It reads both schemas and prints, per channel, how the last session ended and every gap
between sessions that no complete backfill run covers. Exit code 1 means a gap is still open in a
channel that asked for backfill — the deploy runbook in the README says what to do about it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

from doomtp_bot.config import Settings
from doomtp_bot.history.backfill import MIN_GAP_MS

RECENT_DEFAULT = 7


def _open(dsn: str, schema: str) -> psycopg.Connection[dict[str, object]]:
    """A read-only connection pinned to one schema, so a query can't wander into the other."""
    conn = psycopg.connect(dsn, row_factory=dict_row)
    conn.execute("SET default_transaction_read_only = on")
    conn.execute(f'SET search_path TO "{schema}"')
    return conn


def _when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _duration(ms: int) -> str:
    seconds = ms / 1000
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f}m" if seconds < 5400 else f"{seconds / 3600:.1f}h"


def gaps(
    chatlog: psycopg.Connection[dict[str, object]], channel_id: str, since_ms: int
) -> list[tuple[int, int]]:
    """Between one session's end and the next one's start — the same rule the bot fills by."""
    rows = chatlog.execute(
        "SELECT started_at, ended_at FROM log_sessions WHERE channel_id = %s ORDER BY started_at",
        (channel_id,),
    ).fetchall()
    sessions = [(int(r["started_at"]), r["ended_at"]) for r in rows]
    found = []
    for (_, ended_at), (next_start, _) in zip(sessions, sessions[1:], strict=False):
        if ended_at is None:  # still open: the next startup closes it
            continue
        if next_start - int(ended_at) >= MIN_GAP_MS and next_start >= since_ms:
            found.append((int(ended_at), next_start))
    return found


def filled(chatlog: psycopg.Connection[dict[str, object]], channel_id: str, gap: tuple[int, int]) -> str:
    """Empty if the gap is covered, otherwise why it isn't."""
    row = chatlog.execute(
        "SELECT complete, error, inserted FROM backfill_runs"
        " WHERE channel_id = %s AND gap_from = %s AND gap_to = %s ORDER BY at DESC LIMIT 1",
        (channel_id, *gap),
    ).fetchone()
    if row is None:
        return "no backfill run"
    if row["error"]:
        return f"backfill failed: {row['error']}"
    if not row["complete"]:
        return f"backfill incomplete ({row['inserted']} messages)"
    return ""


def report(dsn: str, recent_days: int) -> tuple[list[str], int]:
    """The lines to print, and how many gaps are still open in channels that asked for backfill."""
    lines: list[str] = []
    say = lines.append
    since_ms = int(datetime.now(UTC).timestamp() * 1000) - recent_days * 86_400_000
    open_gaps = 0
    with closing(_open(dsn, "bot")) as bot, closing(_open(dsn, "chatlog")) as chatlog:
        channels = bot.execute(
            "SELECT channel_id, login, history_backfill FROM channels WHERE active ORDER BY login"
        ).fetchall()
        for channel in channels:
            last = chatlog.execute(
                "SELECT started_at, ended_at, end_reason FROM log_sessions"
                " WHERE channel_id = %s ORDER BY started_at DESC LIMIT 1",
                (channel["channel_id"],),
            ).fetchone()
            if last is None:
                say(f"{channel['login']}: never logged")
                continue
            if last["ended_at"] is None:
                state = f"listening since {_when(int(last['started_at']))}"
            else:
                state = f"last session ended {_when(int(last['ended_at']))} ({last['end_reason']})"
            found = gaps(chatlog, channel["channel_id"], since_ms)
            if not channel["history_backfill"]:  # gaps there are expected, so they're a count, not a list
                say(f"{channel['login']}: {state}, backfill off ({len(found)} gaps, none filled)")
                continue
            say(f"{channel['login']}: {state}, backfill on")
            for gap in found:
                why = filled(chatlog, channel["channel_id"], gap)
                where = f"  {_when(gap[0])} + {_duration(gap[1] - gap[0])}"
                open_gaps += bool(why)
                say(f"{where}: OPEN — {why}" if why else f"{where}: filled")
    if open_gaps:
        say(f"{open_gaps} gap(s) still open in the last {recent_days} days")
    return lines, open_gaps


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--database-url", default=None, help="Postgres URL (default: the bot's own DATABASE_URL)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=RECENT_DEFAULT,
        help=f"how far back to look for gaps (default {RECENT_DEFAULT})",
    )
    args = parser.parse_args(argv)
    dsn = args.database_url or Settings().database_dsn()
    try:
        lines, open_gaps = report(dsn, args.days)
    except psycopg.OperationalError as exc:
        print(f"cannot reach the database: {exc}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 1 if open_gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
