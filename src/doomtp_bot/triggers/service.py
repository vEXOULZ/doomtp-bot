"""Triggers, listeners and timers: storage plus matching (architecture §7).

A trigger is an expression the channel runs when something happens: a raid, a sub, a chat line matching
a regex, or a clock. Everything runs through the same runtime as a typed command — preflight, cooldowns,
the moderation index and the Outbox — at the rank the moderator who created it chose.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.core import capabilities
from doomtp_bot.lang import SYNTAX_VERSION
from doomtp_bot.lang.ast import invocations
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import Context, ParserParams, parse
from doomtp_bot.patterns import PatternError, compile_pattern, search
from doomtp_bot.policy.roles import BUILTIN_RANKS, GLOBAL
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
MAX_COOLDOWN_S = 24 * 3600
# Triggers that fire with nobody behind them, so a per-chatter cooldown has no one to hold (ADR-0031).
NO_CHATTER: frozenset[str] = frozenset({"timer", "cron", "stream_online", "stream_offline"})
# A pack's own triggers (ADR-0032) fire in every channel that has the pack, so they are held tighter than a
# channel's: at most this many a pack, a clock no more often than every 15 minutes, and at everyone's rank.
MAX_PACK_TRIGGERS = 10
MIN_PACK_EVERY_S = 15 * 60
PACK_TRIGGER_RANK = BUILTIN_RANKS["everyone"]
PACK_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")


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
    pack_id: str | None = None
    #: Seconds before it fires again anywhere in the channel, and for the same chatter (ADR-0031); 0 is none.
    cooldown_s: int = 0
    user_cooldown_s: int = 0

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


def parse_duration(text: str) -> int:
    """A cooldown: `0`, `30`, `30s`, `2m`, `1h` → seconds, up to 24h. Raises TriggerError."""
    match = re.fullmatch(r"(\d+)\s*([smh]?)", text.strip().lower())
    if match is None:
        raise TriggerError("a cooldown looks like 0, 30s, 2m or 1h")
    seconds = int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    if seconds > MAX_COOLDOWN_S:
        raise TriggerError("a cooldown is at most 24h")
    return seconds


def check_cooldowns(type_: str, cooldown_s: int, user_cooldown_s: int) -> None:
    """Both within 0–24h, and a per-chatter one only where the trigger has a chatter (ADR-0031)."""
    for value in (cooldown_s, user_cooldown_s):
        if not 0 <= value <= MAX_COOLDOWN_S:
            raise TriggerError("a cooldown is between 0 and 24h")
    if user_cooldown_s and type_ in NO_CHATTER:
        raise TriggerError(f"a {type_} trigger has no chatter, so it takes only the channel-wide cooldown")


def check_pack_schedule(type_: str, schedule: dict[str, Any] | None) -> None:
    """A pack's timer or cron fires at most every 15 minutes, wherever the pack is (ADR-0032)."""
    schedule = schedule or {}
    if type_ == "timer" and int(schedule.get("every_s") or 0) < MIN_PACK_EVERY_S:
        raise TriggerError("a pack's timer runs every 15m or less often")
    if type_ == "cron":
        try:
            cron = parse_cron(str(schedule.get("cron", "")))
        except CronError as exc:
            raise TriggerError(str(exc)) from exc
        # The minutes of the day it fires on, around the clock: the smallest gap is how often it can fire.
        times = sorted(h * 60 + m for h in cron.hour for m in cron.minute)
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)] + [times[0] + 1440 - times[-1]]
        if min(gaps) * 60 < MIN_PACK_EVERY_S:
            raise TriggerError("a pack's cron fires at most every 15 minutes")


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
        self._pack_scopes: dict[str, frozenset[str]] = {}  # pack id → channels it is published in, or '*'
        # Whether a name runs in every channel: a built-in or a system pack's member. A pack's own trigger
        # may call those and its pack's public commands, nothing else (ADR-0032). Wired in with the runtime.
        self.everywhere: Callable[[str], bool] = lambda _name: False

    async def reload(self) -> None:
        by_channel: dict[str, list[Trigger]] = {}
        pack_triggers: list[Trigger] = []
        pack_scopes: dict[str, set[str]] = {}
        listeners: dict[int, Pattern] = {}
        crons: dict[int, Cron] = {}
        async with await self.conn.execute(
            "SELECT p.pack_id, p.channel_id FROM custom_command_pack_publications p"
            " JOIN custom_command_packs k ON k.id = p.pack_id WHERE p.status = 'active' AND k.status = 'active'"
        ) as cur:
            for row in await cur.fetchall():
                pack_scopes.setdefault(row["pack_id"], set()).add(row["channel_id"])
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
                    pack_id=row["pack_id"],
                    cooldown_s=row["cooldown_s"],
                    user_cooldown_s=row["user_cooldown_s"],
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
        self._pack_scopes = {pack_id: frozenset(scopes) for pack_id, scopes in pack_scopes.items()}

    def _packs_in(self, channel_id: str) -> list[Trigger]:
        """The pack triggers that apply here (ADR-0029), as copies carrying this channel's id: the pack is
        published here or globally, and its module is on here."""
        found: list[Trigger] = []
        for trigger in self._pack_triggers:
            scopes = self._pack_scopes.get(trigger.pack_id or "", frozenset())
            if GLOBAL not in scopes and channel_id not in scopes:
                continue
            if self.scope is not None and not self.scope.module_on(channel_id, trigger.pack or ""):
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

    def pack_triggers(self, pack_id: str) -> list[Trigger]:
        """A pack's triggers as stored, with channel `*`, wherever they apply."""
        return [t for t in self._pack_triggers if t.pack_id == pack_id]

    def pack_trigger(self, pack_id: str, key: str) -> Trigger | None:
        return next((t for t in self._pack_triggers if t.pack_id == pack_id and t.pack_key == key), None)

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
        cooldown_s: int = 0,
        user_cooldown_s: int = 0,
        created_by: str | None,
        prefix: str,
        via: str,
    ) -> Trigger:
        """Store a trigger. `prefix` is the channel's command sign, which the expression is parsed under."""
        self._check(channel_id, type_, expr, match, schedule, prefix)
        check_cooldowns(type_, cooldown_s, user_cooldown_s)
        async with transaction(self.conn):
            trigger_id = await fetch_value(
                self.conn,
                "INSERT INTO triggers (channel_id, type, match, schedule, expr, syntax_version,"
                " run_as_rank, log_level, cooldown_s, user_cooldown_s, created_by, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (channel_id, type_, json.dumps(match or {}), json.dumps(schedule or {}), expr,
                 SYNTAX_VERSION, run_as_rank, log_level.value, cooldown_s, user_cooldown_s,
                 created_by or "system", now_ms(), now_ms()),
            )  # fmt: skip
            cooldowns = (
                {"cooldown_s": cooldown_s, "user_cooldown_s": user_cooldown_s} if cooldown_s or user_cooldown_s else {}
            )
            await write_audit(
                self.conn,
                action="trigger.add",
                actor_user_id=created_by,
                via=via,
                channel_id=channel_id,
                target=f"{type_}:{trigger_id}",
                after={"expr": expr, "match": match or {}, "schedule": schedule or {}, **cooldowns},
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
        cooldown_s: int | None = None,
        user_cooldown_s: int | None = None,
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
            "cooldown_s": current.cooldown_s if cooldown_s is None else cooldown_s,
            "user_cooldown_s": current.user_cooldown_s if user_cooldown_s is None else user_cooldown_s,
        }
        check_cooldowns(current.type, new["cooldown_s"], new["user_cooldown_s"])
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
                " cooldown_s = %s, user_cooldown_s = %s, syntax_version = %s, updated_at = %s"
                " WHERE id = %s AND channel_id = %s",
                (new["expr"], json.dumps(new["match"]), json.dumps(new["schedule"]), new["run_as_rank"],
                 new["log_level"].value, new["cooldown_s"], new["user_cooldown_s"], SYNTAX_VERSION, now_ms(),
                 trigger_id, channel_id),
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

    # ── pack triggers (ADR-0029, ADR-0032) ──────────────────────────────────
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
        """The pack script adds or replaces the pack's trigger `key`, checked as `add` checks one. True when
        it changed."""
        self._check(GLOBAL, type_, expr, match, schedule, prefix)
        return await self._write_pack_trigger(
            pack_id,
            key,
            {"type": type_, "match": match or {}, "schedule": schedule or {}, "expr": expr,
             "run_as_rank": run_as_rank, "log_level": log_level.value},
            actor_user_id=created_by,
            via=via,
            action="trigger.pack_install",
        )  # fmt: skip

    async def set_pack_trigger(
        self,
        *,
        pack_id: str,
        key: str,
        type_: str,
        expr: str,
        match: dict[str, Any] | None = None,
        schedule: dict[str, Any] | None = None,
        cooldown_s: int | None = None,
        user_cooldown_s: int | None = None,
        commands: Collection[str],
        internal: Collection[str] = (),
        actor_user_id: str,
        via: str,
    ) -> tuple[Trigger, bool]:
        """The pack's owner adds or replaces its trigger `key` (ADR-0032): checked as a channel's trigger is,
        and by the pack's own rules. `commands` are the pack's public members, the only custom commands it
        may call; `internal` its helpers, named in the refusal. It runs at everyone's rank and keeps its
        on/off state when replaced, and its cooldowns unless they are given. Returns it and whether it is
        new. Audited as `trigger.pack_set`."""
        key = key.lower()
        if not PACK_KEY.fullmatch(key):
            raise TriggerError("a trigger's key is a letter, then up to 31 letters, digits or _")
        existing = self.pack_trigger(pack_id, key)
        if existing is None and len(self.pack_triggers(pack_id)) >= MAX_PACK_TRIGGERS:
            raise TriggerError(f"a pack has at most {MAX_PACK_TRIGGERS} triggers")
        self._check(GLOBAL, type_, expr, match, schedule, "!")
        if cooldown_s is None:
            cooldown_s = existing.cooldown_s if existing is not None else 0
        if user_cooldown_s is None:
            user_cooldown_s = existing.user_cooldown_s if existing is not None and type_ not in NO_CHATTER else 0
        check_cooldowns(type_, cooldown_s, user_cooldown_s)
        check_pack_schedule(type_, schedule)
        self._check_pack_calls(expr, type_, commands, internal)
        await self._write_pack_trigger(
            pack_id,
            key,
            {"type": type_, "match": match or {}, "schedule": schedule or {}, "expr": expr,
             "run_as_rank": PACK_TRIGGER_RANK, "log_level": LogLevel.OUTPUT.value,
             "cooldown_s": cooldown_s, "user_cooldown_s": user_cooldown_s},
            actor_user_id=actor_user_id,
            via=via,
            action="trigger.pack_set",
        )  # fmt: skip
        found = self.pack_trigger(pack_id, key)
        assert found is not None
        return found, existing is None

    async def set_pack_trigger_enabled(
        self, *, pack_id: str, key: str, enabled: bool, actor_user_id: str, via: str
    ) -> bool:
        """Turn a pack's trigger on or off everywhere the pack is. False when the pack has no such key."""
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "UPDATE triggers SET enabled = %s, updated_at = %s WHERE pack_id = %s AND pack_key = %s",
                (enabled, now_ms(), pack_id, key.lower()),
            )
            if cur.rowcount:
                await write_audit(
                    self.conn,
                    action="trigger.pack_enable" if enabled else "trigger.pack_disable",
                    actor_user_id=actor_user_id,
                    via=via,
                    channel_id=GLOBAL,
                    target=f"{pack_id}:{key.lower()}",
                )
        if cur.rowcount:
            await self.reload()
        return bool(cur.rowcount)

    async def set_pack_trigger_cooldowns(
        self,
        *,
        pack_id: str,
        key: str,
        cooldown_s: int,
        user_cooldown_s: int | None,
        actor_user_id: str,
        via: str,
    ) -> Trigger | None:
        """A pack trigger's cooldowns (ADR-0031); `user_cooldown_s=None` keeps the per-chatter one. None when
        the pack has no such key. Audited as `trigger.pack_edit`."""
        current = self.pack_trigger(pack_id, key.lower())
        if current is None:
            return None
        user_s = current.user_cooldown_s if user_cooldown_s is None else user_cooldown_s
        check_cooldowns(current.type, cooldown_s, user_s)
        before = {"cooldown_s": current.cooldown_s, "user_cooldown_s": current.user_cooldown_s}
        after = {"cooldown_s": cooldown_s, "user_cooldown_s": user_s}
        if before == after:
            return current
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE triggers SET cooldown_s = %s, user_cooldown_s = %s, updated_at = %s WHERE id = %s",
                (cooldown_s, user_s, now_ms(), current.id),
            )
            await write_audit(
                self.conn,
                action="trigger.pack_edit",
                actor_user_id=actor_user_id,
                via=via,
                channel_id=GLOBAL,
                target=f"{pack_id}:{current.pack_key}",
                before=before,
                after=after,
            )
        await self.reload()
        return self.pack_trigger(pack_id, current.pack_key)

    async def remove_pack_trigger(self, *, pack_id: str, key: str, actor_user_id: str, via: str) -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "DELETE FROM triggers WHERE pack_id = %s AND pack_key = %s", (pack_id, key.lower())
            )
            if cur.rowcount:
                await write_audit(
                    self.conn,
                    action="trigger.pack_remove",
                    actor_user_id=actor_user_id,
                    via=via,
                    channel_id=GLOBAL,
                    target=f"{pack_id}:{key.lower()}",
                )
        if cur.rowcount:
            await self.reload()
        return bool(cur.rowcount)

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

    async def _write_pack_trigger(
        self, pack_id: str, key: str, wanted: dict[str, Any], *, actor_user_id: str, via: str, action: str
    ) -> bool:
        """Insert or update the pack's row for `key` with `wanted`'s columns. True when anything changed."""
        columns = {k: json.dumps(v) if k in ("match", "schedule") else v for k, v in wanted.items()}
        async with transaction(self.conn):
            async with await self.conn.execute(
                "SELECT * FROM triggers WHERE pack_id = %s AND pack_key = %s", (pack_id, key)
            ) as cur:
                row = await cur.fetchone()
            if row is not None and all(row[k] == v for k, v in columns.items()):
                return False
            if row is None:
                names = ", ".join(columns)
                await self.conn.execute(
                    f"INSERT INTO triggers (channel_id, {names}, syntax_version, created_by, created_at, updated_at,"
                    f" pack_id, pack_key) VALUES (%s, {', '.join(['%s'] * len(columns))}, %s, %s, %s, %s, %s, %s)",
                    (GLOBAL, *columns.values(), SYNTAX_VERSION, actor_user_id, now_ms(), now_ms(), pack_id, key),
                )
            else:
                sets = ", ".join(f"{k} = %s" for k in columns)
                await self.conn.execute(
                    f"UPDATE triggers SET {sets}, syntax_version = %s, updated_at = %s WHERE id = %s",
                    (*columns.values(), SYNTAX_VERSION, now_ms(), row["id"]),
                )
            await write_audit(
                self.conn,
                action=action,
                actor_user_id=actor_user_id,
                via=via,
                channel_id=GLOBAL,
                target=f"{pack_id}:{key}",
                after={k: wanted[k] for k in ("type", "expr", "match", "schedule")}
                | {k: wanted[k] for k in ("cooldown_s", "user_cooldown_s") if wanted.get(k)},
            )
        await self.reload()
        return True

    def _check_pack_calls(self, expr: str, type_: str, commands: Collection[str], internal: Collection[str]) -> None:
        """A pack's trigger calls only its pack's public commands and what runs everywhere (ADR-0032): what
        else a name means differs from one channel to the next."""
        context = Context.LISTENER if type_ == "listener" else Context.TRIGGER
        for inv in invocations(parse(expr, context, self.parser_params("!"))):
            if inv.personal:
                raise TriggerError(f"a pack's trigger can't call @{inv.name}: links differ for every chatter")
            if inv.name in commands or self.everywhere(inv.name):
                continue
            if inv.name in internal:
                raise TriggerError(f"{inv.name} is internal: a pack's trigger calls the pack's public commands")
            raise TriggerError(
                f"a pack's trigger calls only its pack's commands and the built-ins, and {inv.name} is neither"
            )

    def _check_expression(self, channel_id: str, expr: str, context: Context, prefix: str) -> None:
        """Parse the expression the way it will run, and filter it: it is read out later (architecture §9)."""
        try:
            parse(expr, context, self.parser_params(prefix))
        except ParseError as exc:
            raise TriggerError(str(exc)) from exc
        hits = self.filters.rejects_any(channel_id, expr) if self.filters is not None else []
        if hits:
            raise TriggerError(f"the filter rejects that: {', '.join(hits)}")
