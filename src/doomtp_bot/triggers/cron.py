"""Cron schedules: "every Friday at 18:00", rather than "every 6 hours" (architecture §7).

A timer answers *how often*; a cron answers *when*. They are the same trigger row with a different
schedule, so everything downstream — rank, preflight, cooldowns, the Outbox — is unchanged.

The five fields are the familiar ones, in the channel's own timezone:

    minute  hour  day-of-month  month  day-of-week
    0       18    *             *      fri

Supported per field: `*`, a number, a name (`fri`, `dec`), a list (`1,15`), a range (`mon-fri`) and a
step (`*/15`, `9-17/2`). Day-of-week accepts 0 or 7 for Sunday, as crontabs do. When both day-of-month
and day-of-week are restricted, either one matching is enough — the historic crontab rule, kept so a
schedule copied from a crontab behaves the way its author expects.

Nothing here parses seconds: the scheduler ticks well under a minute and fires a given minute once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

FIELDS = ("minute", "hour", "day", "month", "weekday")
RANGES = {"minute": (0, 59), "hour": (0, 23), "day": (1, 31), "month": (1, 12), "weekday": (0, 6)}
NAMES = {
    "month": {n: i for i, n in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)},
    "weekday": {n: i for i, n in enumerate(
        ("sun", "mon", "tue", "wed", "thu", "fri", "sat"))},
}  # fmt: skip


class CronError(ValueError):
    """A schedule the user got wrong, phrased for chat."""


@dataclass(frozen=True, slots=True)
class Cron:
    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]
    restricted_day: bool  # day-of-month was given
    restricted_weekday: bool  # day-of-week was given

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.minute or when.hour not in self.hour:
            return False
        if when.month not in self.month:
            return False
        day_ok = when.day in self.day
        weekday_ok = (when.weekday() + 1) % 7 in self.weekday  # Monday is 0 in Python, 1 in cron
        if self.restricted_day and self.restricted_weekday:
            return day_ok or weekday_ok
        return day_ok and weekday_ok


def parse_cron(text: str) -> Cron:
    """`0 18 * * fri` → a Cron. Raises CronError with something a moderator can act on."""
    parts = text.strip().lower().split()
    if len(parts) != 5:
        raise CronError("a cron needs 5 fields: minute hour day month weekday, e.g. 0 18 * * fri")
    values = {name: _field(name, part) for name, part in zip(FIELDS, parts, strict=True)}
    return Cron(
        minute=values["minute"],
        hour=values["hour"],
        day=values["day"],
        month=values["month"],
        weekday=values["weekday"],
        restricted_day=parts[2] != "*",
        restricted_weekday=parts[4] != "*",
    )


def describe(text: str) -> str:
    """A short, human reading of a schedule — what `!timer list` shows next to the expression."""
    parts = text.strip().lower().split()
    if len(parts) != 5:
        return text
    minute, hour, day, month, weekday = parts
    when = f"{hour.zfill(2)}:{minute.zfill(2)}" if minute.isdigit() and hour.isdigit() else text
    if day == month == weekday == "*":
        return f"daily at {when}" if when != text else text
    if weekday != "*" and day == "*":
        return f"{weekday} at {when}"
    return text


def _field(name: str, part: str) -> frozenset[int]:
    low, high = RANGES[name]
    found: set[int] = set()
    for chunk in part.split(","):
        body, _, step_text = chunk.partition("/")
        step = _step(step_text, chunk)
        if body == "*":
            first, last = low, high
        else:
            start_text, _, end_text = body.partition("-")
            first = _value(name, start_text, chunk)
            last = _value(name, end_text, chunk) if end_text else first
            if last < first:
                raise CronError(f"{chunk!r} counts backwards")
        found.update(range(first, last + 1, step))
    normalized = {v % 7 if name == "weekday" else v for v in found}
    if any(not low <= v <= high for v in normalized):
        raise CronError(f"{name} must be between {low} and {high}")
    return frozenset(normalized)


def _step(text: str, chunk: str) -> int:
    if not text:
        return 1
    if not text.isdigit() or int(text) < 1:
        raise CronError(f"{chunk!r} has a bad step; write it like */15")
    return int(text)


def _value(name: str, text: str, chunk: str) -> int:
    named = NAMES.get(name, {}).get(text)
    if named is not None:
        return named
    if text.isdigit():
        value = int(text)
        if name == "weekday" and value == 7:  # both 0 and 7 mean Sunday
            return 0
        return value
    known = ", ".join(NAMES[name]) if name in NAMES else "a number"
    raise CronError(f"{chunk!r} isn't a {name}; use {known}")
