"""Cron schedules: parsing, matching, and firing in the channel's timezone (architecture §7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from doomtp_bot.triggers.cron import CronError, describe, parse_cron


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# ── parsing ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("schedule", "when", "fires"),
    [
        ("0 18 * * *", "2026-09-19T18:00", True),  # daily at six
        ("0 18 * * *", "2026-09-19T18:01", False),
        ("*/15 * * * *", "2026-09-19T13:30", True),  # every quarter hour
        ("*/15 * * * *", "2026-09-19T13:31", False),
        ("0 18 * * fri", "2026-09-18T18:00", True),  # a Friday
        ("0 18 * * fri", "2026-09-19T18:00", False),  # a Saturday
        ("0 18 * * 5", "2026-09-18T18:00", True),  # Friday by number
        ("30 9 * * mon-fri", "2026-09-21T09:30", True),  # a weekday range
        ("30 9 * * mon-fri", "2026-09-20T09:30", False),  # Sunday
        ("0 0 1 * *", "2026-10-01T00:00", True),  # the first of the month
        ("0 0 1 * *", "2026-10-02T00:00", False),
        ("0 12 * dec sun", "2026-12-06T12:00", True),  # December, and a Sunday
        ("0 12 * dec sun", "2026-12-07T12:00", False),  # December, but a Monday
        ("0 0 * * 0", "2026-09-20T00:00", True),  # Sunday as 0
        ("0 0 * * 7", "2026-09-20T00:00", True),  # …and as 7
        ("0 9,17 * * *", "2026-09-19T17:00", True),  # a list
    ],
)
def test_schedules_match_the_times_they_name(schedule: str, when: str, fires: bool) -> None:
    assert parse_cron(schedule).matches(at(when)) is fires


def test_day_of_month_and_weekday_together_match_either(schedule: str = "0 0 13 * fri") -> None:
    """The crontab rule: with both restricted, either one is enough (Friday the 13th is not required)."""
    cron = parse_cron(schedule)
    assert cron.matches(at("2026-11-13T00:00"))  # the 13th, and a Friday
    assert cron.matches(at("2026-09-13T00:00"))  # the 13th, a Sunday
    assert cron.matches(at("2026-09-18T00:00"))  # a Friday, not the 13th
    assert not cron.matches(at("2026-09-17T00:00"))


@pytest.mark.parametrize(
    "schedule",
    ["", "0 18 * *", "0 18 * * * *", "60 * * * *", "* 25 * * *", "0 0 * * xyz", "0 0 32 * *", "*/0 * * * *"],
)
def test_impossible_schedules_are_refused(schedule: str) -> None:
    with pytest.raises(CronError):
        parse_cron(schedule)


def test_a_schedule_reads_back_in_words() -> None:
    assert describe("0 18 * * *") == "daily at 18:00"
    assert describe("0 18 * * fri") == "fri at 18:00"
    assert describe("*/15 * * * *") == "*/15 * * * *"  # nothing clearer to say than the schedule itself
