"""Triggers, listeners and timers: storage plus matching (architecture §7).

A trigger is an expression the channel runs when something happens: a raid, a sub, a chat line matching
a regex, or a clock. Everything runs through the same runtime as a typed command — preflight, cooldowns,
the moderation index and the Outbox — at the rank the moderator who created it chose.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.core import capabilities
from doomtp_bot.lang import SYNTAX_VERSION
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import Context, ParserParams, parse
from doomtp_bot.patterns import PatternError, compile_pattern, search
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.runtime.spec import LogLevel
from doomtp_bot.storage.db import Connection, fetch_value, transaction
from doomtp_bot.triggers.cron import Cron, CronError, parse_cron

if TYPE_CHECKING:
    from doomtp_bot.filters.service import FilterService
    from doomtp_bot.patterns import Pattern

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
    "pyramid",
    "timer",
    "cron",
    "listener",
]
TRIGGER_TYPES: tuple[TriggerType, ...] = (
    "redemption", "raid", "sub", "resub", "gift_sub", "cheer", "follow",
    "stream_online", "stream_offline", "pyramid", "timer", "cron", "listener",
)  # fmt: skip
# `pyramid` comes from a chat watcher (ADR-0028), not from Twitch: `watchers.PyramidWatcher`.
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


class PackTriggerError(TriggerError):
    """A pack's trigger can't be changed from a channel: its pack's script owns it (ADR-0029)."""

    def __init__(self, trigger: Trigger) -> None:
        super().__init__(
            f"trigger {trigger.id} belongs to the {trigger.pack} pack; turn it off with !module disable {trigger.pack}"
        )


class PackScope(Protocol):
    """Where a pack's triggers apply (ADR-0029): `PolicyService` in the bot."""

    def channels(self) -> Iterable[Any]: ...

    def module_on(self, channel_id: str, module: str) -> bool: ...


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
    #: The pack this trigger belongs to (ADR-0029), by name, or None for a channel's own trigger. A pack
    #: trigger is handed out as a copy per channel where it applies, with that channel's id.
    pack: str | None = None
    pack_key: str = ""

    @property
    def regex(self) -> str:
        return str(self.match.get("regex", ""))

    @property
    def name(self) -> str:
        """A listener's name (ADR-0019), or "" for one made with `!trigger listen`, which had none."""
        return str(self.match.get("name", ""))

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


def compile_listener(pattern: str) -> Pattern:
    """Compile a listener regex, refusing what is too long or invalid (architecture §7)."""
    if not pattern or len(pattern) > MAX_REGEX_CHARS:
        raise TriggerError(f"listener patterns must be 1–{MAX_REGEX_CHARS} characters")
    try:
        return compile_pattern(pattern)
    except PatternError as exc:
        raise TriggerError(f"invalid regex: {exc}") from exc


def match_fields(pattern: Pattern, text: str) -> dict[str, Any] | None:
    """`{match.1}`, `{match.name}` and `{match.0}` for a listener hit, or None if it doesn't match (or
    gives up: see `doomtp_bot.patterns`)."""
    found = search(pattern, text)
    if found is None:
        return None
    fields: dict[str, Any] = {"0": found.group(0)}
    for index, value in enumerate(found.groups(), start=1):
        fields[str(index)] = value if value is not None else ""
    fields.update({name: value or "" for name, value in found.groupdict().items()})
    return fields


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, LogLevel) else value


class TriggerService:
    """Storage plus an in-memory copy, like the policy snapshot."""

    def __init__(
        self, conn: Connection, *, filters: FilterService | None = None, scope: PackScope | None = None
    ) -> None:
        self.conn = conn
        # Which channels a pack's triggers reach, and whether its module is on there. Without it (scripts),
        # a pack trigger applies wherever its pack is published, and no clock runs it.
        self.scope = scope
        # An expression is stored only if it parses and the channel's filter accepts it, whoever sends it.
        self.filters = filters
        # The runtime's parser settings, wired in once it exists; until then, the parser's own defaults.
        self.parser_params: Callable[[str], ParserParams] = lambda prefix: ParserParams(prefix=prefix)
        self._by_channel: dict[str, list[Trigger]] = {}
        self._listeners: dict[int, Pattern] = {}
        self._crons: dict[int, Cron] = {}
        self._pack_triggers: list[Trigger] = []
        self._pack_scopes: dict[str, frozenset[str]] = {}  # pack name → channels it is published in, or '*'

    async def reload(self) -> None:
        by_channel: dict[str, list[Trigger]] = {}
        pack_triggers: list[Trigger] = []
        pack_scopes: dict[str, set[str]] = {}
        listeners: dict[int, Pattern] = {}
        crons: dict[int, Cron] = {}
        async with await self.conn.execute(
            "SELECT k.name, p.channel_id FROM custom_command_pack_publications p"
            " JOIN custom_command_packs k ON k.id = p.pack_id WHERE p.status = 'active' AND k.status = 'active'"
        ) as cur:
            for row in await cur.fetchall():
                pack_scopes.setdefault(row["name"], set()).add(row["channel_id"])
        async with await self.conn.execute(
            "SELECT t.*, k.name AS pack_name FROM triggers t LEFT JOIN custom_command_packs k ON k.id = t.pack_id"
            " WHERE t.pack_id IS NULL OR k.status = 'active' ORDER BY t.id"
        ) as cur:
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
                    pack=row["pack_name"],
                    pack_key=row["pack_key"] or "",
                )
                if trigger.pack is not None:
                    pack_triggers.append(trigger)
                else:
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
        self._pack_triggers = pack_triggers
        self._pack_scopes = {name: frozenset(scopes) for name, scopes in pack_scopes.items()}

    def _packs_in(self, channel_id: str) -> list[Trigger]:
        """The pack triggers that apply here (ADR-0029), as copies carrying this channel's id: the pack is
        published here or globally, and its module is on here."""
        found: list[Trigger] = []
        for trigger in self._pack_triggers:
            pack = trigger.pack or ""
            scopes = self._pack_scopes.get(pack, frozenset())
            if GLOBAL not in scopes and channel_id not in scopes:
                continue
            if self.scope is not None and not self.scope.module_on(channel_id, pack):
                continue
            found.append(dataclasses.replace(trigger, channel_id=channel_id))
        return found

    def _every_channel(self) -> list[Trigger]:
        """Every channel's own triggers, and each pack trigger once per active channel it applies in."""
        found = [t for group in self._by_channel.values() for t in group]
        if self._pack_triggers and self.scope is not None:
            for settings in self.scope.channels():
                if settings.active:
                    found.extend(self._packs_in(settings.channel_id))
        return found

    def in_channel(self, channel_id: str) -> list[Trigger]:
        """The channel's own triggers, then the pack triggers that apply here (marked by `pack`)."""
        return [*self._by_channel.get(channel_id, ()), *self._packs_in(channel_id)]

    def pack_triggers(self, pack: str) -> list[Trigger]:
        """A pack's triggers as stored, with channel `*`, wherever they apply."""
        return [t for t in self._pack_triggers if t.pack == pack]

    def of_type(self, channel_id: str, type_: str) -> list[Trigger]:
        return [t for t in self.in_channel(channel_id) if t.type == type_ and t.enabled]

    def timers(self) -> list[Trigger]:
        return [t for t in self._every_channel() if t.type == "timer" and t.enabled]

    def crons(self) -> list[Trigger]:
        """Cron triggers with a schedule that parsed — the scheduler walks these every tick."""
        return [t for t in self._every_channel() if t.type == "cron" and t.enabled and t.id in self._crons]

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
        if "reward_id" in conditions and str(payload.get("reward", {}).get("id")) != str(conditions["reward_id"]):
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
        prefix: str,
        via: str,
    ) -> Trigger:
        """Store a trigger. `prefix` is the channel's command sign, which the expression is parsed under."""
        self._check(channel_id, type_, expr, match, schedule, prefix)
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
                via=via,
                channel_id=channel_id,
                target=f"{type_}:{trigger_id}",
                after={"expr": expr, "match": match or {}, "schedule": schedule or {}},
            )
        await self.reload()
        found = next(t for t in self.in_channel(channel_id) if t.id == int(trigger_id or 0))
        return found

    async def update(
        self,
        *,
        channel_id: str,
        trigger_id: int,
        expr: str | None = None,
        match: dict[str, Any] | None = None,
        schedule: dict[str, Any] | None = None,
        run_as_rank: int | None = None,
        log_level: LogLevel | None = None,
        actor_user_id: str | None,
        prefix: str,
        via: str,
    ) -> Trigger | None:
        """Change a trigger in place, checked as `add` checks a new one; a field left None stays. Its type
        can't change. None when there's no such trigger in `channel_id`. Audited as `trigger.edit`."""
        current = self._own(channel_id, trigger_id)
        if current is None:
            return None
        new: dict[str, Any] = {
            "expr": current.expr if expr is None else expr,
            "match": current.match if match is None else match,
            "schedule": current.schedule if schedule is None else schedule,
            "run_as_rank": current.run_as_rank if run_as_rank is None else run_as_rank,
            "log_level": current.log_level if log_level is None else log_level,
        }
        if current.type == "listener":
            compile_listener(str(new["match"].get("regex", "")))
        if current.type == "timer" and not new["schedule"].get("every_s"):
            raise TriggerError("a timer needs an interval, e.g. every 15m")
        if current.type == "cron":
            try:
                parse_cron(str(new["schedule"].get("cron", "")))
            except CronError as exc:
                raise TriggerError(str(exc)) from exc
        if expr is not None:
            context = Context.LISTENER if current.type == "listener" else Context.TRIGGER
            self._check_expression(channel_id, expr, context, prefix)
        before = {k: getattr(current, k) for k in new}
        changed = {k: v for k, v in new.items() if before[k] != v}
        if not changed:
            return current
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE triggers SET expr = %s, match = %s, schedule = %s, run_as_rank = %s, log_level = %s,"
                " syntax_version = %s, updated_at = %s WHERE id = %s AND channel_id = %s",
                (new["expr"], json.dumps(new["match"]), json.dumps(new["schedule"]), new["run_as_rank"],
                 new["log_level"].value, SYNTAX_VERSION, now_ms(), trigger_id, channel_id),
            )  # fmt: skip
            await write_audit(
                self.conn,
                action="trigger.edit",
                actor_user_id=actor_user_id,
                via=via,
                channel_id=channel_id,
                target=f"{current.type}:{trigger_id}",
                before={k: _plain(before[k]) for k in changed},
                after={k: _plain(v) for k, v in changed.items()},
            )
        await self.reload()
        return next(t for t in self.in_channel(channel_id) if t.id == trigger_id)

    async def remove(self, *, channel_id: str, trigger_id: int, actor_user_id: str | None, via: str) -> bool:
        self._own(channel_id, trigger_id)  # refuses a pack's trigger
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "DELETE FROM triggers WHERE id = %s AND channel_id = %s", (trigger_id, channel_id)
            )
            if cur.rowcount:
                await write_audit(
                    self.conn,
                    action="trigger.remove",
                    actor_user_id=actor_user_id,
                    via=via,
                    channel_id=channel_id,
                    target=str(trigger_id),
                )
        if cur.rowcount:
            await self.reload()
        return bool(cur.rowcount)

    async def set_enabled(
        self, *, channel_id: str, trigger_id: int, enabled: bool, actor_user_id: str | None, via: str
    ) -> bool:
        self._own(channel_id, trigger_id)  # refuses a pack's trigger
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
                    via=via,
                    channel_id=channel_id,
                    target=str(trigger_id),
                )
        if cur.rowcount:
            await self.reload()
        return bool(cur.rowcount)

    def _own(self, channel_id: str, trigger_id: int) -> Trigger | None:
        """The channel's own trigger with this id. A pack trigger that applies here raises
        `PackTriggerError`: chat and the API can't change it (ADR-0029)."""
        for trigger in self.in_channel(channel_id):
            if trigger.id == trigger_id:
                if trigger.pack is not None:
                    raise PackTriggerError(trigger)
                return trigger
        return None

    def _check(
        self,
        channel_id: str,
        type_: str,
        expr: str,
        match: dict[str, Any] | None,
        schedule: dict[str, Any] | None,
        prefix: str,
    ) -> None:
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
        self._check_expression(channel_id, expr, Context.LISTENER if type_ == "listener" else Context.TRIGGER, prefix)

    # ── pack triggers (ADR-0029): written only by the script that owns the pack ──
    async def install_pack_trigger(
        self,
        *,
        pack_id: str,
        key: str,
        type_: str,
        expr: str,
        match: dict[str, Any] | None = None,
        schedule: dict[str, Any] | None = None,
        run_as_rank: int,
        log_level: LogLevel = LogLevel.OUTPUT,
        created_by: str,
        prefix: str = "!",
        via: str = "script",
    ) -> bool:
        """Add or replace the pack's trigger `key`, checked as `add` checks one. True when it changed."""
        self._check(GLOBAL, type_, expr, match, schedule, prefix)
        wanted = {
            "type": type_,
            "match": json.dumps(match or {}),
            "schedule": json.dumps(schedule or {}),
            "expr": expr,
            "run_as_rank": run_as_rank,
            "log_level": log_level.value,
        }
        async with transaction(self.conn):
            async with await self.conn.execute(
                "SELECT * FROM triggers WHERE pack_id = %s AND pack_key = %s", (pack_id, key)
            ) as cur:
                row = await cur.fetchone()
            if row is not None and all(row[k] == v for k, v in wanted.items()):
                return False
            if row is None:
                await self.conn.execute(
                    "INSERT INTO triggers (channel_id, type, match, schedule, expr, run_as_rank, log_level,"
                    " syntax_version, created_by, created_at, updated_at, pack_id, pack_key)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (GLOBAL, *wanted.values(), SYNTAX_VERSION, created_by, now_ms(), now_ms(), pack_id, key),
                )
            else:
                await self.conn.execute(
                    "UPDATE triggers SET type = %s, match = %s, schedule = %s, expr = %s, run_as_rank = %s,"
                    " log_level = %s, syntax_version = %s, updated_at = %s WHERE id = %s",
                    (*wanted.values(), SYNTAX_VERSION, now_ms(), row["id"]),
                )
            await write_audit(
                self.conn,
                action="trigger.pack_install",
                actor_user_id=created_by,
                via=via,
                channel_id=GLOBAL,
                target=f"{pack_id}:{key}",
                after={"type": type_, "expr": expr, "match": match or {}, "schedule": schedule or {}},
            )
        await self.reload()
        return True

    async def remove_pack_triggers(
        self, *, pack_id: str, keep: Iterable[str], actor_user_id: str, via: str = "script"
    ) -> list[str]:
        """Delete the pack's triggers whose key isn't in `keep`. Returns the keys removed."""
        async with transaction(self.conn):
            async with await self.conn.execute(
                "DELETE FROM triggers WHERE pack_id = %s AND NOT (pack_key = ANY(%s)) RETURNING pack_key",
                (pack_id, list(keep)),
            ) as cur:
                removed = [row["pack_key"] for row in await cur.fetchall()]
            for key in removed:
                await write_audit(
                    self.conn,
                    action="trigger.pack_remove",
                    actor_user_id=actor_user_id,
                    via=via,
                    channel_id=GLOBAL,
                    target=f"{pack_id}:{key}",
                )
        if removed:
            await self.reload()
        return removed

    def _check_expression(self, channel_id: str, expr: str, context: Context, prefix: str) -> None:
        """Parse the expression the way it will run, and filter it: it is read out later (architecture §9)."""
        try:
            parse(expr, context, self.parser_params(prefix))
        except ParseError as exc:
            raise TriggerError(str(exc)) from exc
        hits = self.filters.rejects_any(channel_id, expr) if self.filters is not None else []
        if hits:
            raise TriggerError(f"the filter rejects that: {', '.join(hits)}")
