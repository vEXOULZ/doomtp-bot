"""Triggers, listeners and timers: storage plus matching (architecture §7).

A trigger is an expression the channel runs when something happens: a raid, a sub, a chat line matching
a regex, or a clock. Everything runs through the same runtime as a typed command — preflight, cooldowns,
the moderation index and the Outbox — at the rank the moderator who created it chose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

import structlog

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.core import capabilities
from doomtp_bot.lang import SYNTAX_VERSION
from doomtp_bot.runtime.spec import LogLevel
from doomtp_bot.storage.db import Connection, fetch_value, transaction
from doomtp_bot.triggers.cron import Cron, CronError, parse_cron

log = structlog.get_logger(__name__)

TriggerType = Literal[
    "redemption",
    "raid",
    "sub",
    "resub",
    "gift_sub",
    "cheer",
    "follow",
    "stream_online",
    "stream_offline",
    "timer",
    "cron",
    "listener",
]
TRIGGER_TYPES: tuple[TriggerType, ...] = (
    "redemption", "raid", "sub", "resub", "gift_sub", "cheer", "follow",
    "stream_online", "stream_offline", "timer", "cron", "listener",
)  # fmt: skip
# Event types that only work where the channel granted the bot something (ADR-0007): `follow` needs the
# bot to be a moderator, the other two need the broadcaster to have connected their channel. The rest —
# raids, subs, resubs, gift subs — arrive as chat notifications, which every joined channel has.
REQUIRED_CAPABILITY: dict[str, str] = {
    "follow": capabilities.FOLLOWERS,
    "redemption": capabilities.REDEMPTIONS,
    "cheer": capabilities.BITS,
}
MAX_REGEX_CHARS = 200
MAX_TIMER_EVERY_S = 24 * 3600
MIN_TIMER_EVERY_S = 60


class TriggerError(ValueError):
    """Something the user got wrong: unknown type, bad regex, impossible schedule."""


@dataclass(frozen=True, slots=True)
class Trigger:
    id: int
    channel_id: str
    type: TriggerType
    match: dict[str, Any]
    schedule: dict[str, Any]
    expr: str
    run_as_rank: int
    enabled: bool
    log_level: LogLevel
    created_by: str

    @property
    def regex(self) -> str:
        return str(self.match.get("regex", ""))

    @property
    def every_s(self) -> int:
        return int(self.schedule.get("every_s", 0))

    @property
    def cron(self) -> str:
        return str(self.schedule.get("cron", ""))


def parse_every(text: str) -> int:
    """`15m`, `90s`, `2h` → seconds. Raises TriggerError."""
    match = re.fullmatch(r"(\d+)\s*([smh]?)", text.strip().lower())
    if match is None:
        raise TriggerError("interval looks like 90s, 15m or 2h")
    seconds = int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    if not MIN_TIMER_EVERY_S <= seconds <= MAX_TIMER_EVERY_S:
        raise TriggerError(f"interval must be between {MIN_TIMER_EVERY_S}s and 24h")
    return seconds


def compile_listener(pattern: str) -> re.Pattern[str]:
    """Compile a listener regex, refusing what is too long or invalid (architecture §7)."""
    if not pattern or len(pattern) > MAX_REGEX_CHARS:
        raise TriggerError(f"listener patterns must be 1–{MAX_REGEX_CHARS} characters")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise TriggerError(f"invalid regex: {exc}") from exc


def match_fields(pattern: re.Pattern[str], text: str) -> dict[str, Any] | None:
    """`{match.1}`, `{match.name}` and `{match.0}` for a listener hit, or None if it doesn't match."""
    found = pattern.search(text)
    if found is None:
        return None
    fields: dict[str, Any] = {"0": found.group(0)}
    for index, value in enumerate(found.groups(), start=1):
        fields[str(index)] = value if value is not None else ""
    fields.update({name: value or "" for name, value in found.groupdict().items()})
    return fields


class TriggerService:
    """Storage plus an in-memory copy, like the policy snapshot."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._by_channel: dict[str, list[Trigger]] = {}
        self._listeners: dict[int, re.Pattern[str]] = {}
        self._crons: dict[int, Cron] = {}

    async def reload(self) -> None:
        by_channel: dict[str, list[Trigger]] = {}
        listeners: dict[int, re.Pattern[str]] = {}
        crons: dict[int, Cron] = {}
        async with await self.conn.execute("SELECT * FROM triggers ORDER BY id") as cur:
            for row in await cur.fetchall():
                trigger = Trigger(
                    id=row["id"],
                    channel_id=row["channel_id"],
                    type=row["type"],
                    match=json.loads(row["match"] or "{}"),
                    schedule=json.loads(row["schedule"] or "{}"),
                    expr=row["expr"],
                    run_as_rank=row["run_as_rank"],
                    enabled=bool(row["enabled"]),
                    log_level=LogLevel(row["log_level"]),
                    created_by=row["created_by"],
                )
                by_channel.setdefault(trigger.channel_id, []).append(trigger)
                if trigger.type == "listener" and trigger.enabled:
                    try:
                        listeners[trigger.id] = compile_listener(trigger.regex)
                    except TriggerError:
                        log.warning("trigger.bad_listener", trigger=trigger.id)
                if trigger.type == "cron" and trigger.enabled:
                    try:
                        crons[trigger.id] = parse_cron(trigger.cron)
                    except CronError:
                        log.warning("trigger.bad_cron", trigger=trigger.id)
        self._by_channel, self._listeners, self._crons = by_channel, listeners, crons

    def in_channel(self, channel_id: str) -> list[Trigger]:
        return list(self._by_channel.get(channel_id, ()))

    def of_type(self, channel_id: str, type_: str) -> list[Trigger]:
        return [t for t in self._by_channel.get(channel_id, ()) if t.type == type_ and t.enabled]

    def timers(self) -> list[Trigger]:
        return [t for group in self._by_channel.values() for t in group if t.type == "timer" and t.enabled]

    def crons(self) -> list[Trigger]:
        """Cron triggers with a schedule that parsed — the scheduler walks these every tick."""
        return [
            t
            for group in self._by_channel.values()
            for t in group
            if t.type == "cron" and t.enabled and t.id in self._crons
        ]

    def cron_for(self, trigger_id: int) -> Cron | None:
        return self._crons.get(trigger_id)

    def listeners_matching(self, channel_id: str, text: str) -> list[tuple[Trigger, dict[str, Any]]]:
        """Listeners whose regex matches, with their capture fields (architecture §7)."""
        hits: list[tuple[Trigger, dict[str, Any]]] = []
        for trigger in self.of_type(channel_id, "listener"):
            pattern = self._listeners.get(trigger.id)
            if pattern is None:
                continue
            fields = match_fields(pattern, text)
            if fields is not None:
                hits.append((trigger, fields))
        return hits

    def event_triggers(self, channel_id: str, type_: str, payload: dict[str, Any]) -> list[Trigger]:
        """Event triggers of this type whose match conditions the payload satisfies."""
        found: list[Trigger] = []
        for trigger in self.of_type(channel_id, type_):
            if self._matches(trigger.match, payload):
                found.append(trigger)
        return found

    @staticmethod
    def _matches(conditions: dict[str, Any], payload: dict[str, Any]) -> bool:
        if "reward_id" in conditions and str(payload.get("reward", {}).get("id")) != str(
            conditions["reward_id"]
        ):
            return False
        for key, field in (("min_viewers", "viewers"), ("min_bits", "bits"), ("min_months", "months")):
            if key in conditions and int(payload.get(field) or 0) < int(conditions[key]):
                return False
        return True

    # ── management ──────────────────────────────────────────────────────────
    async def add(
        self,
        *,
        channel_id: str,
        type_: str,
        expr: str,
        match: dict[str, Any] | None = None,
        schedule: dict[str, Any] | None = None,
        run_as_rank: int,
        log_level: LogLevel = LogLevel.OUTPUT,
        created_by: str | None,
    ) -> Trigger:
        if type_ not in TRIGGER_TYPES:
            raise TriggerError(f"type must be one of: {', '.join(TRIGGER_TYPES)}")
        if type_ == "listener":
            compile_listener(str((match or {}).get("regex", "")))
        if type_ == "timer" and not (schedule or {}).get("every_s"):
            raise TriggerError("a timer needs an interval, e.g. every 15m")
        if type_ == "cron":
            try:
                parse_cron(str((schedule or {}).get("cron", "")))
            except CronError as exc:
                raise TriggerError(str(exc)) from exc
        async with transaction(self.conn):
            trigger_id = await fetch_value(
                self.conn,
                "INSERT INTO triggers (channel_id, type, match, schedule, expr, syntax_version,"
                " run_as_rank, log_level, created_by, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (channel_id, type_, json.dumps(match or {}), json.dumps(schedule or {}), expr,
                 SYNTAX_VERSION, run_as_rank, log_level.value, created_by or "system", now_ms(), now_ms()),
            )  # fmt: skip
            await write_audit(
                self.conn,
                action="trigger.add",
                actor_user_id=created_by,
                via="chat",
                channel_id=channel_id,
                target=f"{type_}:{trigger_id}",
                after={"expr": expr, "match": match or {}, "schedule": schedule or {}},
            )
        await self.reload()
        found = next(t for t in self.in_channel(channel_id) if t.id == int(trigger_id or 0))
        return found

    async def remove(self, *, channel_id: str, trigger_id: int, actor_user_id: str | None) -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "DELETE FROM triggers WHERE id = %s AND channel_id = %s", (trigger_id, channel_id)
            )
            if cur.rowcount:
                await write_audit(
                    self.conn,
                    action="trigger.remove",
                    actor_user_id=actor_user_id,
                    via="chat",
                    channel_id=channel_id,
                    target=str(trigger_id),
                )
        if cur.rowcount:
            await self.reload()
        return bool(cur.rowcount)

    async def set_enabled(
        self, *, channel_id: str, trigger_id: int, enabled: bool, actor_user_id: str | None
    ) -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "UPDATE triggers SET enabled = %s, updated_at = %s WHERE id = %s AND channel_id = %s",
                (enabled, now_ms(), trigger_id, channel_id),
            )
            if cur.rowcount:
                await write_audit(
                    self.conn,
                    action="trigger.enable" if enabled else "trigger.disable",
                    actor_user_id=actor_user_id,
                    via="chat",
                    channel_id=channel_id,
                    target=str(trigger_id),
                )
        if cur.rowcount:
            await self.reload()
        return bool(cur.rowcount)
