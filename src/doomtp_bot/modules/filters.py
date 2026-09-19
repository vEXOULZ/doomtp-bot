"""`filters` module: `!filter` — the channel's badword list (architecture §9).

The list censors everything the bot says, and rejects content users try to store. Channel moderators
manage their own list; bot admins can add entries that apply everywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from doomtp_bot.filters.matcher import Action, FilterError, Kind
from doomtp_bot.filters.service import ACTIONS, KINDS
from doomtp_bot.modules._common import rank
from doomtp_bot.policy.roles import BOT_ADMIN_RANK, GLOBAL
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Code, CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Example, LogLevel, Param

if TYPE_CHECKING:
    from doomtp_bot.filters.service import FilterService

MODULE = "core_admin"  # managed with the other channel settings, so it can't be disabled
USAGE = (
    "filter list | add <word|wildcard|regex|allow> <pattern> [mask|replace|tag|block] [replacement]"
    " [global] | rm <id> | on|off <id> | test <text>"
)


def _filters(ctx: CommandContext) -> FilterService:
    return ctx.service("filters")  # type: ignore[no-any-return]


def _scope(ctx: CommandContext, words: list[str]) -> str:
    if words and words[-1].lower() == "global":
        if rank(ctx) < BOT_ADMIN_RANK:
            raise CommandError("only bot admins can change the global list", Code.DENIED)
        return GLOBAL
    return ctx.channel.id


def _need(values: list[str], count: int) -> None:
    if len(values) < count:
        raise CommandError(f"usage: {USAGE}")


@command(
    CommandSpec(
        name="filter",
        module=MODULE,
        toggleable=False,
        summary="Manage the channel's word filter",
        description=USAGE,
        params=(Param("1+", "arguments", description=USAGE),),
        required_role="moderator",
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example("{sign}filter add word badword mask", "filtering badword (mask), entry 3"),
            Example("{sign}filter test you are a badword", "would send: you are a *******"),
        ),
    )
)
async def filter_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    service = _filters(ctx)
    values = list(args.values)
    _need(values, 1)
    action = values[0].lower()
    actor_id = ctx.invoker.id if ctx.invoker else None

    if action == "list":
        entries = service.entries_for(ctx.channel.id)
        if not entries:
            return Result.success("no filters here", [])
        listing = ", ".join(
            f"{e.id}:{e.pattern} ({e.kind}/{e.action}{'' if e.enabled else ', off'}"
            f"{', global' if e.channel_id == GLOBAL else ''})"
            for e in entries
        )
        return Result.success(listing, [{"id": e.id, "pattern": e.pattern, "kind": e.kind} for e in entries])

    if action == "test":
        _need(values, 2)
        text = " ".join(values[1:])
        result = service.check(ctx.channel.id, text)
        if result.blocked:
            return Result.success(f"blocked by: {', '.join(result.patterns())}", {"blocked": True})
        if not result.changed:
            return Result.success("nothing would be filtered", {"blocked": False, "hits": []})
        return Result.success(
            f"would send: {result.text} (matched {', '.join(result.patterns())})",
            {"blocked": False, "text": result.text, "hits": result.patterns()},
        )

    scope = _scope(ctx, values)
    if action == "add":
        _need(values, 3)
        pattern = values[2]
        if values[1].lower() not in KINDS:
            raise CommandError(f"kind must be one of: {', '.join(KINDS)}")
        kind = cast("Kind", values[1].lower())
        rest = [w for w in values[3:] if w.lower() != "global"]
        named_action = bool(rest) and rest[0].lower() in ACTIONS
        entry_action = cast("Action", rest[0].lower() if named_action else "mask")
        replacement = " ".join(rest[1:] if named_action else rest)
        try:
            entry = await service.add(
                channel_id=scope,
                pattern=pattern,
                kind=kind,
                action=entry_action,
                replacement=replacement,
                actor_user_id=actor_id,
            )
        except FilterError as exc:
            raise CommandError(str(exc)) from exc
        where = "everywhere" if scope == GLOBAL else "here"
        return Result.success(
            f"filtering {pattern} ({kind}/{entry_action}) {where}, entry {entry.id}", {"id": entry.id}
        )

    _need(values, 2)
    if not values[1].isdigit():
        raise CommandError(f"give the entry id from {ctx.channel.prefix}filter list")
    entry_id = int(values[1])
    if action == "rm":
        removed = await service.remove(channel_id=scope, entry_id=entry_id, actor_user_id=actor_id)
        if not removed:
            raise CommandError(f"no entry {entry_id} in this list")
        return Result.success(f"removed entry {entry_id}")
    if action in ("on", "off"):
        changed = await service.set_enabled(
            channel_id=scope, entry_id=entry_id, enabled=action == "on", actor_user_id=actor_id
        )
        if not changed:
            raise CommandError(f"no entry {entry_id} in this list")
        return Result.success(f"entry {entry_id} is {'on' if action == 'on' else 'off'}")
    raise CommandError(f"usage: {USAGE}")


COMMANDS: tuple[Command, ...] = (filter_cmd,)
