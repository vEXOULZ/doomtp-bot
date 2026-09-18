"""`triggers` module: `!trigger` and `!timer` (architecture §7).

Both write the same table. A trigger runs on an event or a chat line; a timer runs on a clock. The
expression is taken raw, parsed before it is stored, and runs at the creator's rank — never above it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import Context, parse
from doomtp_bot.modules._common import rank
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Example, LogLevel, Param
from doomtp_bot.triggers.service import (
    SUPPORTED_NOW,
    TRIGGER_TYPES,
    TriggerError,
    compile_listener,
    parse_every,
)

if TYPE_CHECKING:
    from doomtp_bot.triggers.service import Trigger, TriggerService

MODULE = "triggers"
# `listen` takes everything after it raw, so a regex keeps its backslashes; this splits the two halves.
LISTEN_SEPARATOR = "=>"
TRIGGER_USAGE = (
    "trigger list | add <" + "|".join(TRIGGER_TYPES[:9]) + "> <expression> |"
    " listen <regex> => <expression> | rm <id> | on|off <id>"
)
TIMER_USAGE = (
    "timer list | add <every> [jitter=<d>] [only_live] [min_lines=<n>] <expression> | rm <id> | on|off <id>"
)


def _service(ctx: CommandContext) -> TriggerService:
    return ctx.service("triggers")  # type: ignore[no-any-return]


def _need(values: list[str], count: int, usage: str) -> None:
    if len(values) < count:
        raise CommandError(f"usage: {usage}")


def _check_expression(ctx: CommandContext, expr: str, context: Context) -> None:
    runtime = ctx.service("runtime")
    try:
        parse(expr, context, runtime.parser_params(ctx.channel.prefix))
    except ParseError as exc:
        raise CommandError(str(exc)) from exc
    filters = ctx.exec.services.get("filters")
    hits = filters.rejects(ctx.channel.id, expr) if filters is not None else []
    if hits:
        raise CommandError(f"the filter rejects that: {', '.join(hits)}")


def _describe(trigger: Trigger) -> str:
    what = trigger.regex if trigger.type == "listener" else trigger.type
    if trigger.type == "timer":
        what = f"every {trigger.every_s}s"
    return f"{trigger.id}:{what}{'' if trigger.enabled else ' (off)'} → {trigger.expr}"


async def _listing(ctx: CommandContext, types: tuple[str, ...]) -> Result:
    found = [t for t in _service(ctx).in_channel(ctx.channel.id) if t.type in types]
    if not found:
        return Result.success("none here", [])
    return Result.success(
        "; ".join(_describe(t) for t in found),
        [{"id": t.id, "type": t.type, "enabled": t.enabled, "expr": t.expr} for t in found],
    )


async def _remove_or_toggle(ctx: CommandContext, action: str, values: list[str], usage: str) -> Result:
    _need(values, 2, usage)
    if not values[1].isdigit():
        raise CommandError("give the id from the list")
    actor = ctx.invoker.id if ctx.invoker else None
    entry_id = int(values[1])
    if action == "rm":
        removed = await _service(ctx).remove(
            channel_id=ctx.channel.id, trigger_id=entry_id, actor_user_id=actor
        )
        if not removed:
            raise CommandError(f"no entry {entry_id} here")
        return Result.success(f"removed {entry_id}")
    changed = await _service(ctx).set_enabled(
        channel_id=ctx.channel.id, trigger_id=entry_id, enabled=action == "on", actor_user_id=actor
    )
    if not changed:
        raise CommandError(f"no entry {entry_id} here")
    return Result.success(f"{entry_id} is {'on' if action == 'on' else 'off'}")


@command(
    CommandSpec(
        name="trigger",
        module=MODULE,
        summary="Run an expression when something happens in the channel",
        description=TRIGGER_USAGE,
        params=(Param("1+", "arguments", description=TRIGGER_USAGE),),
        required_role="moderator",
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example("!trigger add raid echo welcome {event.user.name} and {event.viewers} raiders!", ""),
            Example(r"!trigger listen \bhello\b => echo hi {chatter.display}", ""),
        ),
    ),
    raw_tail_subcommands=(("add", 3), ("listen", 2)),
)
async def trigger_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    values = list(args.values)
    _need(values, 1, TRIGGER_USAGE)
    action = values[0].lower()
    if action == "list":
        return await _listing(ctx, tuple(t for t in TRIGGER_TYPES if t != "timer"))
    if action in ("rm", "on", "off"):
        return await _remove_or_toggle(ctx, action, values, TRIGGER_USAGE)

    # `add` and `listen` take the rest of the line raw (spec §3.3), so it arrives in args.raw_tail:
    # `add` after the event type, `listen` right away, so a regex keeps its backslashes.
    expr = (args.raw_tail or " ".join(values[2:])).strip()
    if not expr:
        raise CommandError(f"usage: {TRIGGER_USAGE}")
    match: dict[str, Any] = {}
    if action == "listen":
        pattern, separator, body = expr.partition(f" {LISTEN_SEPARATOR} ")
        if not separator or not body.strip():
            raise CommandError(f"usage: {TRIGGER_USAGE}")
        type_, match["regex"], expr = "listener", pattern.strip(), body.strip()
        try:
            compile_listener(match["regex"])
        except TriggerError as exc:
            raise CommandError(str(exc)) from exc
        _check_expression(ctx, expr, Context.LISTENER)
    elif action == "add":
        _need(values, 2, TRIGGER_USAGE)
        type_ = values[1].lower()
        if type_ not in TRIGGER_TYPES or type_ in ("listener", "timer"):
            raise CommandError(f"usage: {TRIGGER_USAGE}")
        _check_expression(ctx, expr, Context.TRIGGER)
    else:
        raise CommandError(f"usage: {TRIGGER_USAGE}")

    try:
        created = await _service(ctx).add(
            channel_id=ctx.channel.id,
            type_=type_,
            expr=expr,
            match=match,
            run_as_rank=rank(ctx),  # never above the moderator who created it
            created_by=ctx.invoker.id if ctx.invoker else None,
        )
    except TriggerError as exc:
        raise CommandError(str(exc)) from exc
    note = "" if type_ in SUPPORTED_NOW else f" ⚠ {type_} events need channel authorization the bot lacks"
    return Result.success(f"added {type_} trigger {created.id}{note}", {"id": created.id})


@command(
    CommandSpec(
        name="timer",
        module=MODULE,
        summary="Run an expression on a schedule",
        description=TIMER_USAGE,
        params=(Param("1+", "arguments", description=TIMER_USAGE),),
        required_role="moderator",
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("!timer add 15m min_lines=10 echo remember to hydrate", ""),),
    ),
    raw_tail_subcommands=(("add", 3),),
)
async def timer_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    values = list(args.values)
    _need(values, 1, TIMER_USAGE)
    action = values[0].lower()
    if action == "list":
        return await _listing(ctx, ("timer",))
    if action in ("rm", "on", "off"):
        return await _remove_or_toggle(ctx, action, values, TIMER_USAGE)
    if action != "add":
        raise CommandError(f"usage: {TIMER_USAGE}")

    _need(values, 2, TIMER_USAGE)
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
    _check_expression(ctx, expr, Context.TRIGGER)
    try:
        created = await _service(ctx).add(
            channel_id=ctx.channel.id,
            type_="timer",
            expr=expr,
            schedule=schedule,
            run_as_rank=rank(ctx),
            created_by=ctx.invoker.id if ctx.invoker else None,
        )
    except TriggerError as exc:
        raise CommandError(str(exc)) from exc
    every = schedule["every_s"]
    return Result.success(f"timer {created.id} every {every}s", {"id": created.id, "every_s": every})


COMMANDS: tuple[Command, ...] = (trigger_cmd, timer_cmd)
