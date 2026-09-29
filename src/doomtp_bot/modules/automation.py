"""`automation` module: `!listen`, `!event` and `!timer` (architecture §7, ADR-0019).

All three write the same `triggers` table. A listener runs on a chat line its regex matches, an event on
something Twitch reports, and a timer on a clock. The expression is taken raw, parsed before it is
stored, and runs at the creator's rank — never above it. `!trigger` is the old name for listeners and
events together, kept as a deprecated alias for one release.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from doomtp_bot.modules._common import need, rank
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Example, LogLevel, Param
from doomtp_bot.triggers.cron import describe as describe_cron
from doomtp_bot.triggers.service import (
    REQUIRED_CAPABILITY,
    TRIGGER_TYPES,
    TriggerError,
    parse_every,
)

if TYPE_CHECKING:
    from doomtp_bot.triggers.service import Trigger, TriggerService

MODULE = "automation"
EVENT_TYPES = tuple(t for t in TRIGGER_TYPES if t not in ("listener", "timer", "cron"))
LISTENER_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
LISTEN_USAGE = 'listen list | add <name> </regex/|"regex"> <expression> | test <text> | rm|on|off <name|id>'
EVENT_USAGE = "event list | add <" + "|".join(EVENT_TYPES) + "> <expression> | rm|on|off <id>"
TIMER_USAGE = (
    "timer list | add <every> [jitter=<d>] [only_live] [min_lines=<n>] <expression>"
    ' | cron "<m h dom mon dow>" <expression> | rm|on|off <id>'
)
TRIGGER_USAGE = (
    "trigger list | add <" + "|".join(EVENT_TYPES) + "> <expression> |"
    ' listen <regex|"regex with spaces"> <expression> | rm <id> | on|off <id>'
)


def _leading_arg(raw: str, usage: str, *, slashes: bool = False) -> tuple[str, str]:
    """Split a raw tail into its first argument and the expression after it (ADR-0018: no `=>`).

    The argument is one word, `"…"` when it has spaces, or `/…/` for a regex when `slashes` is set.
    Nothing inside is unescaped, so a regex keeps its backslashes; a quoted argument ends at the first
    closing mark followed by a space.
    """
    raw = raw.strip()
    closer = raw[:1] if raw[:1] == '"' or (slashes and raw[:1] == "/") else ""
    if closer:
        end = raw.find(closer + " ", 1)
        if end < 0:
            raise CommandError(f"usage: {usage}")
        arg, rest = raw[1:end], raw[end + 2 :]
    else:
        if " => " in f" {raw} ":
            quoted = next(part for part in usage.split(" | ") if '"' in part)
            raise CommandError(f"=> is gone: quote the first argument instead, e.g. {quoted}")
        arg, _, rest = raw.partition(" ")
    if not arg or not rest.strip():
        raise CommandError(f"usage: {usage}")
    return arg, rest.strip()


def _service(ctx: CommandContext) -> TriggerService:
    return ctx.service("triggers")  # type: ignore[no-any-return]


def _describe(trigger: Trigger) -> str:
    if trigger.type == "listener":
        what = f"{trigger.name} /{trigger.regex}/" if trigger.name else f"/{trigger.regex}/"
    elif trigger.type == "timer":
        what = f"every {trigger.every_s}s"
    elif trigger.type == "cron":
        what = describe_cron(trigger.cron)
    else:
        what = trigger.type
    return f"{trigger.id}:{what}{'' if trigger.enabled else ' (off)'} → {trigger.expr}"


async def _listing(ctx: CommandContext, types: tuple[str, ...]) -> Result:
    found = [t for t in _service(ctx).in_channel(ctx.channel.id) if t.type in types]
    if not found:
        return Result.success("none here", [])
    return Result.success(
        "; ".join(_describe(t) for t in found),
        [
            {"id": t.id, "type": t.type, "enabled": t.enabled, "expr": t.expr}
            | ({"name": t.name} if t.name else {})
            for t in found
        ],
    )


def _listener(ctx: CommandContext, name: str) -> Trigger | None:
    return next((t for t in _service(ctx).of_type(ctx.channel.id, "listener") if t.name == name), None)


def _entry_id(ctx: CommandContext, given: str, *, named: bool) -> int:
    """The id a `rm`/`on`/`off` names: its number, or for a listener its name."""
    if given.isdigit():
        return int(given)
    if named:
        found = _listener(ctx, given.lower())
        if found is not None:
            return found.id
        raise CommandError(f"no listener called {given} here")
    raise CommandError("give the id from the list")


async def _remove_or_toggle(
    ctx: CommandContext, action: str, values: list[str], usage: str, *, named: bool = False
) -> Result:
    need(values, 2, usage)
    entry_id = _entry_id(ctx, values[1], named=named)
    actor = ctx.invoker.id if ctx.invoker else None
    if action == "rm":
        removed = await _service(ctx).remove(
            channel_id=ctx.channel.id, trigger_id=entry_id, actor_user_id=actor, via="chat"
        )
        if not removed:
            raise CommandError(f"no entry {entry_id} here")
        return Result.success(f"removed {values[1]}")
    changed = await _service(ctx).set_enabled(
        channel_id=ctx.channel.id,
        trigger_id=entry_id,
        enabled=action == "on",
        actor_user_id=actor,
        via="chat",
    )
    if not changed:
        raise CommandError(f"no entry {entry_id} here")
    return Result.success(f"{values[1]} is {'on' if action == 'on' else 'off'}")


async def _add(
    ctx: CommandContext,
    type_: str,
    expr: str,
    *,
    match: dict[str, Any] | None = None,
    schedule: dict[str, Any] | None = None,
) -> Trigger:
    try:
        return await _service(ctx).add(
            channel_id=ctx.channel.id,
            type_=type_,
            expr=expr,
            match=match,
            schedule=schedule,
            run_as_rank=rank(ctx),  # never above the moderator who created it
            created_by=ctx.invoker.id if ctx.invoker else None,
            prefix=ctx.channel.prefix,
            via="chat",
        )
    except TriggerError as exc:
        raise CommandError(str(exc)) from exc


def _warning(ctx: CommandContext, type_: str) -> str:
    """Say up front when a trigger is stored but can't fire here yet (ADR-0007)."""
    needed = REQUIRED_CAPABILITY.get(type_)
    if needed and needed not in ctx.channel.capabilities:
        how = "mod the bot" if needed == "followers" else "the broadcaster has to connect the channel"
        return f" ⚠ it stays quiet until this channel grants {needed}: {how}"
    return ""


# ── !listen ─────────────────────────────────────────────────────────────────
@command(
    CommandSpec(
        name="listen",
        module=MODULE,
        summary="Run an expression when a chat line matches a regex",
        description=LISTEN_USAGE,
        params=(Param("1+", "arguments", description=LISTEN_USAGE),),
        required_role="moderator",
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example(r"{sign}listen add hello /\bhello\b/ echo hi {$chatter.display}", ""),
            Example(
                r"{sign}listen add intro /my name is (?P<name>\w+)/ echo nice to meet you {match.name}", ""
            ),
            Example("{sign}listen test hello there", ""),
        ),
    ),
    raw_tail_subcommands=(("add", 3), ("test", 2)),
)
async def listen_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    values = list(args.values)
    need(values, 1, LISTEN_USAGE)
    action = values[0].lower()
    if action == "list":
        return await _listing(ctx, ("listener",))
    if action in ("rm", "on", "off"):
        return await _remove_or_toggle(ctx, action, values, LISTEN_USAGE, named=True)
    if action == "test":
        return _test(ctx, (args.raw_tail or " ".join(values[1:])).strip())
    if action != "add":
        raise CommandError(f"usage: {LISTEN_USAGE}")

    need(values, 2, LISTEN_USAGE)
    name = values[1].lower()
    if not LISTENER_NAME.fullmatch(name):
        raise CommandError("a listener's name is a letter, then up to 31 letters, digits or _")
    if _listener(ctx, name) is not None:
        raise CommandError(f"there's already a listener called {name}: rm it first")
    regex, expr = _leading_arg(args.raw_tail or " ".join(values[2:]), LISTEN_USAGE, slashes=True)
    created = await _add(ctx, "listener", expr, match={"regex": regex, "name": name})
    return Result.success(f"listener {name} added ({created.id})", {"id": created.id, "name": name})


def _test(ctx: CommandContext, text: str) -> Result:
    """Which listeners a chat line would set off, with what they capture — nothing runs."""
    if not text:
        raise CommandError(f"usage: {LISTEN_USAGE}")
    hits = _service(ctx).listeners_matching(ctx.channel.id, text)
    if not hits:
        return Result.success("no listener matches that", [])
    shown = []
    for trigger, fields in hits:
        captures = ", ".join(f"match.{k}={v}" for k, v in fields.items() if k != "0")
        shown.append(f"{trigger.name or trigger.id}" + (f" ({captures})" if captures else ""))
    return Result.success(
        "matches: " + "; ".join(shown),
        [{"id": t.id, "name": t.name, "match": fields} for t, fields in hits],
    )


# ── !event ──────────────────────────────────────────────────────────────────
@command(
    CommandSpec(
        name="event",
        module=MODULE,
        summary="Run an expression when something happens on the channel",
        description=EVENT_USAGE,
        params=(Param("1+", "arguments", description=EVENT_USAGE),),
        required_role="moderator",
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example("{sign}event add raid echo welcome {event.user.name} and {event.viewers} raiders!", ""),
        ),
    ),
    raw_tail_subcommands=(("add", 3),),
)
async def event_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    values = list(args.values)
    need(values, 1, EVENT_USAGE)
    action = values[0].lower()
    if action == "list":
        return await _listing(ctx, EVENT_TYPES)
    if action in ("rm", "on", "off"):
        return await _remove_or_toggle(ctx, action, values, EVENT_USAGE)
    if action != "add":
        raise CommandError(f"usage: {EVENT_USAGE}")
    need(values, 2, EVENT_USAGE)
    type_ = values[1].lower()
    if type_ not in EVENT_TYPES:
        raise CommandError(f"usage: {EVENT_USAGE}")
    expr = (args.raw_tail or " ".join(values[2:])).strip()
    if not expr:
        raise CommandError(f"usage: {EVENT_USAGE}")
    created = await _add(ctx, type_, expr)
    return Result.success(f"{type_} event {created.id} added{_warning(ctx, type_)}", {"id": created.id})


# ── !timer ──────────────────────────────────────────────────────────────────
@command(
    CommandSpec(
        name="timer",
        module=MODULE,
        summary="Run an expression on a schedule",
        description=TIMER_USAGE,
        params=(Param("1+", "arguments", description=TIMER_USAGE),),
        required_role="moderator",
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example("{sign}timer add 15m min_lines=10 echo remember to hydrate", ""),
            Example('{sign}timer cron "0 18 * * fri" echo the stream starts now', ""),
        ),
    ),
    raw_tail_subcommands=(("add", 3), ("cron", 2)),
)
async def timer_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    values = list(args.values)
    need(values, 1, TIMER_USAGE)
    action = values[0].lower()
    if action == "list":
        return await _listing(ctx, ("timer", "cron"))
    if action in ("rm", "on", "off"):
        return await _remove_or_toggle(ctx, action, values, TIMER_USAGE)
    if action == "cron":
        return await _add_cron(ctx, args, values)
    if action != "add":
        raise CommandError(f"usage: {TIMER_USAGE}")

    need(values, 2, TIMER_USAGE)
    try:
        schedule: dict[str, Any] = {"every_s": parse_every(values[1])}
    except TriggerError as exc:
        raise CommandError(str(exc)) from exc
    rest = (args.raw_tail or " ".join(values[2:])).strip()
    expr_words: list[str] = []
    for word in rest.split():
        lowered = word.lower()
        if lowered == "only_live":
            schedule["only_live"] = True
        elif lowered.startswith("jitter="):
            try:
                schedule["jitter_s"] = parse_every(word.split("=", 1)[1])
            except TriggerError as exc:
                raise CommandError(str(exc)) from exc
        elif lowered.startswith("min_lines=") and word.split("=", 1)[1].isdigit():
            schedule["min_chat_lines"] = int(word.split("=", 1)[1])
        else:
            expr_words.append(word)
    expr = " ".join(expr_words)
    if not expr:
        raise CommandError(f"usage: {TIMER_USAGE}")
    created = await _add(ctx, "timer", expr, schedule=schedule)
    every = schedule["every_s"]
    return Result.success(f"timer {created.id} every {every}s", {"id": created.id, "every_s": every})


async def _add_cron(ctx: CommandContext, args: Args, values: list[str]) -> Result:
    """`timer cron "0 18 * * fri" echo we live at six`, in the channel's timezone."""
    schedule_text, expr = _leading_arg(args.raw_tail or " ".join(values[1:]), TIMER_USAGE)
    created = await _add(ctx, "cron", expr, schedule={"cron": schedule_text.strip()})
    when = describe_cron(created.cron)
    return Result.success(
        f"cron {created.id}: {when} ({ctx.channel.timezone})",
        {"id": created.id, "cron": created.cron, "timezone": ctx.channel.timezone},
    )


# ── !trigger (deprecated) ───────────────────────────────────────────────────
@command(
    CommandSpec(
        name="trigger",
        module=MODULE,
        summary="Old name for listen and event, going away after this release",
        description=TRIGGER_USAGE,
        params=(Param("1+", "arguments", description=TRIGGER_USAGE),),
        required_role="moderator",
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}trigger add raid echo welcome raiders!", ""),),
    ),
    raw_tail_subcommands=(("add", 3), ("listen", 2)),
)
async def trigger_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    """The pre-ADR-0019 grammar, unchanged: `listen` makes an unnamed listener, `add` an event."""
    values = list(args.values)
    need(values, 1, TRIGGER_USAGE)
    action = values[0].lower()
    if action == "list":
        return await _listing(ctx, (*EVENT_TYPES, "listener"))
    if action in ("rm", "on", "off"):
        return await _remove_or_toggle(ctx, action, values, TRIGGER_USAGE)

    # `add` and `listen` take the rest of the line raw (spec §3.3), so it arrives in args.raw_tail:
    # `add` after the event type, `listen` right away, so a regex keeps its backslashes.
    expr = (args.raw_tail or " ".join(values[2:])).strip()
    if not expr:
        raise CommandError(f"usage: {TRIGGER_USAGE}")
    match: dict[str, Any] = {}
    if action == "listen":
        type_ = "listener"
        match["regex"], expr = _leading_arg(expr, TRIGGER_USAGE)
    elif action == "add":
        need(values, 2, TRIGGER_USAGE)
        type_ = values[1].lower()
        if type_ not in EVENT_TYPES:
            raise CommandError(f"usage: {TRIGGER_USAGE}")
    else:
        raise CommandError(f"usage: {TRIGGER_USAGE}")
    created = await _add(ctx, type_, expr, match=match)
    return Result.success(
        f"added {type_} trigger {created.id}{_warning(ctx, type_)}{_going_away(ctx.channel.prefix)}",
        {"id": created.id},
    )


def _going_away(sign: str) -> str:
    return f". {sign}trigger is going away: use {sign}listen or {sign}event"


COMMANDS: tuple[Command, ...] = (listen_cmd, event_cmd, timer_cmd, trigger_cmd)
