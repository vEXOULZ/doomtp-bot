"""The deploy check: what the chat log covers and which gaps backfill never closed (ADR-0008)."""

from __future__ import annotations

import time

from doomtp_bot.storage.db import Databases
from scripts.coverage import report

HOUR = 3_600_000
NOW = int(time.time() * 1000)


async def _channel(dbs: Databases, channel_id: str, login: str, *, backfill: bool) -> None:
    await dbs.bot.execute(
        "INSERT INTO channels (channel_id, login, history_backfill, added_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (channel_id, login, backfill, NOW, NOW),
    )


async def _session(dbs: Databases, channel_id: str, started: int, ended: int | None, why: str) -> None:
    await dbs.chatlog.execute(
        "INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason) VALUES (%s, %s, %s, %s)",
        (channel_id, started, ended, why),
    )


async def test_it_names_the_gaps_backfill_never_closed(committed_database: tuple[str, Databases]) -> None:
    dsn, dbs = committed_database
    await _channel(dbs, "c1", "alice", backfill=True)
    await _session(dbs, "c1", NOW - 5 * HOUR, NOW - 4 * HOUR, "shutdown")
    await _session(dbs, "c1", NOW - 3 * HOUR, NOW - 2 * HOUR, "shutdown")  # gap: one hour, unfilled
    await _session(dbs, "c1", NOW - HOUR, None, "")  # still listening
    await dbs.chatlog.execute(
        "INSERT INTO backfill_runs (channel_id, gap_from, gap_to, inserted, complete, at)"
        " VALUES ('c1', %s, %s, 12, true, %s)",
        (NOW - 4 * HOUR, NOW - 3 * HOUR, NOW),
    )

    lines, open_gaps = report(dsn, recent_days=7)

    assert open_gaps == 1
    assert lines[0].startswith("alice: listening since ")
    assert lines[0].endswith("backfill on")
    assert lines[1].endswith(": filled")
    assert lines[2].endswith(": OPEN — no backfill run")
    assert lines[-1] == "1 gap(s) still open in the last 7 days"


async def test_gaps_are_counted_not_listed_where_backfill_is_off(
    committed_database: tuple[str, Databases],
) -> None:
    dsn, dbs = committed_database
    await _channel(dbs, "c2", "bob", backfill=False)
    await _session(dbs, "c2", NOW - 5 * HOUR, NOW - 4 * HOUR, "shutdown")
    await _session(dbs, "c2", NOW - 3 * HOUR, NOW - 2 * HOUR, "unclean_shutdown")

    lines, open_gaps = report(dsn, recent_days=7)

    assert open_gaps == 0  # a channel that didn't ask for backfill can't fail the check
    assert len(lines) == 1
    assert "(unclean_shutdown), backfill off (1 gaps, none filled)" in lines[0]


async def test_old_gaps_and_silent_channels_stay_out_of_the_way(
    committed_database: tuple[str, Databases],
) -> None:
    dsn, dbs = committed_database
    await _channel(dbs, "c3", "carol", backfill=True)
    await _session(dbs, "c3", NOW - 900 * HOUR, NOW - 899 * HOUR, "shutdown")
    await _session(dbs, "c3", NOW - 800 * HOUR, NOW - 799 * HOUR, "shutdown")  # gap, but months ago
    await _channel(dbs, "c4", "dave", backfill=True)  # joined, never logged a line

    lines, open_gaps = report(dsn, recent_days=7)

    assert open_gaps == 0
    assert lines[-1] == "dave: never logged"
    assert not any("OPEN" in line for line in lines)
