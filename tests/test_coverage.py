"""The deploy check: what the chat log covers and which gaps backfill never closed (ADR-0008)."""

from __future__ import annotations

import time

import pytest
from psycopg.types.json import Jsonb

import scripts.coverage
from doomtp_bot.storage.db import Databases, fetch_value
from scripts.coverage import main, report

HOUR = 3_600_000
NOW = int(time.time() * 1000)


async def _channel(dbs: Databases, channel_id: str, login: str, *, backfill: bool) -> None:
    await dbs.bot.execute(
        "INSERT INTO channels (channel_id, login, history_backfill, added_at, updated_at) VALUES (%s, %s, %s, %s, %s)",
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

    lines, open_gaps, waiting = report(dsn, recent_days=7)

    assert (open_gaps, waiting) == (1, 0)
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

    lines, open_gaps, waiting = report(dsn, recent_days=7)

    assert (open_gaps, waiting) == (0, 0)  # a channel that didn't ask for backfill can't fail the check
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

    lines, open_gaps, waiting = report(dsn, recent_days=7)

    assert (open_gaps, waiting) == (0, 0)
    assert lines[-1] == "dave: never logged"
    assert not any("OPEN" in line for line in lines)


async def _unfillable_gap(dbs: Databases) -> None:
    """A channel with one filled gap and one the history service said it doesn't log."""
    await _channel(dbs, "c6", "frank", backfill=True)
    await _session(dbs, "c6", NOW - 7 * HOUR, NOW - 6 * HOUR, "shutdown")
    await _session(dbs, "c6", NOW - 5 * HOUR, NOW - 4 * HOUR, "shutdown")
    await _session(dbs, "c6", NOW - 3 * HOUR, None, "")
    await dbs.chatlog.execute(
        "INSERT INTO backfill_runs (channel_id, gap_from, gap_to, inserted, complete, error, at)"
        " VALUES ('c6', %s, %s, 3, true, '', %s), ('c6', %s, %s, 0, false, 'channel_not_logged', %s)",
        (NOW - 6 * HOUR, NOW - 5 * HOUR, NOW, NOW - 4 * HOUR, NOW - 3 * HOUR, NOW),
    )


async def test_a_gap_the_service_cannot_fill_is_listed_but_not_open(
    committed_database: tuple[str, Databases],
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn, dbs = committed_database
    await _unfillable_gap(dbs)

    lines, open_gaps, waiting = report(dsn, recent_days=7)

    assert (open_gaps, waiting) == (0, 0)
    assert lines[1].endswith(": filled")
    assert lines[2].endswith(": UNFILLABLE — the history service doesn't log this channel")
    assert lines[-1] == "1 gap(s) unfillable: the history service doesn't log the channel (not counted as open)"
    assert main(["--database-url", dsn]) == 0
    assert "UNFILLABLE" in capsys.readouterr().out


async def test_strict_counts_unfillable_gaps_as_open(committed_database: tuple[str, Databases]) -> None:
    dsn, dbs = committed_database
    await _unfillable_gap(dbs)

    lines, open_gaps, waiting = report(dsn, recent_days=7, strict=True)

    assert (open_gaps, waiting) == (1, 0)
    assert lines[-2] == "1 gap(s) still open in the last 7 days"
    assert lines[-1].endswith("(counted as open (--strict))")
    assert main(["--database-url", dsn, "--strict"]) == 1


async def test_a_fillable_gap_still_fails_next_to_an_unfillable_one(
    committed_database: tuple[str, Databases],
) -> None:
    dsn, dbs = committed_database
    await _unfillable_gap(dbs)
    await _channel(dbs, "c7", "gina", backfill=True)
    await _session(dbs, "c7", NOW - 5 * HOUR, NOW - 4 * HOUR, "shutdown")
    await _session(dbs, "c7", NOW - 3 * HOUR, None, "")
    await dbs.chatlog.execute(
        "INSERT INTO backfill_runs (channel_id, gap_from, gap_to, complete, error, at)"
        " VALUES ('c7', %s, %s, false, 'http_503', %s)",
        (NOW - 4 * HOUR, NOW - 3 * HOUR, NOW),
    )

    lines, open_gaps, waiting = report(dsn, recent_days=7)

    assert (open_gaps, waiting) == (1, 0)
    assert any(line.endswith(": OPEN — backfill failed: http_503") for line in lines)
    assert main(["--database-url", dsn]) == 1


async def _queued_gap(dbs: Databases) -> int:
    """A channel with one gap that a queued backfill job is waiting to fill; the job's id."""
    await _channel(dbs, "c5", "erin", backfill=True)
    await _session(dbs, "c5", NOW - 5 * HOUR, NOW - 4 * HOUR, "shutdown")
    await _session(dbs, "c5", NOW - 3 * HOUR, None, "")
    payload = {"channel_id": "c5", "kind": "gaps", "from_ms": NOW - 5 * HOUR, "to_ms": NOW - 3 * HOUR}
    job_id = await fetch_value(
        dbs.bot,
        "INSERT INTO jobs.job_runs (kind, subject, state, payload, actor_kind, via)"
        " VALUES ('chat_backfill', 'channel:c5', 'queued', %s, 'system', 'system') RETURNING id",
        (Jsonb(payload),),
    )
    return int(job_id)


async def test_a_gap_a_job_is_still_to_fill_says_so(committed_database: tuple[str, Databases]) -> None:
    dsn, dbs = committed_database
    job_id = await _queued_gap(dbs)

    lines, open_gaps, waiting = report(dsn, recent_days=7)

    assert (open_gaps, waiting) == (1, 1)
    assert lines[1].endswith(f": OPEN — queued as backfill job #{job_id}")
    assert lines[-1] == "1 gap(s) still open in the last 7 days, 1 waiting on backfill jobs"


async def test_wait_gives_up_on_a_job_that_never_runs(
    committed_database: tuple[str, Databases],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn, dbs = committed_database
    await _queued_gap(dbs)
    monkeypatch.setattr(scripts.coverage, "POLL_S", 0.1)

    started = time.monotonic()
    assert main(["--database-url", dsn, "--wait", "1"]) == 1

    assert 1 <= time.monotonic() - started < 10
    assert "waiting on backfill jobs" in capsys.readouterr().out
