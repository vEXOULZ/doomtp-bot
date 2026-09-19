"""`variables` module: !var — read, write and rank variables (ADR-0010, variable-access-matrix.md §3, §5).

!var acts as the typed expression: its writes follow the Typed column and go through the run's write buffer,
so they commit atomically with the rest of the line.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

from doomtp_bot.modules._common import rank, user_arg
from doomtp_bot.policy.roles import BOT_ADMIN_RANK
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.namespaces import CHATTER_KEY, VAR_NAMESPACES
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Code, CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, LogLevel, Param
from doomtp_bot.runtime.values import MISSING, descend, render
from doomtp_bot.runtime.variables import Space, VarKey, WriteOp, key_for

if TYPE_CHECKING:
    from doomtp_bot.variables.store import SqliteVariableStore

MODULE = "variables"
USAGE = (
    "var get <ns.name> [user] | set <ns.name> <value> | incr <ns.name> [amount] | del <ns.name> [user]"
    " | list <ns> [user] | top <ns.name> [count]"
)
_NS_LONGEST_FIRST = sorted(VAR_NAMESPACES, key=len, reverse=True)


def parse_ref(token: str, *, allow_path: bool = False) -> tuple[str, str, tuple[str, ...]]:
    """'channel.chatter.points.best' → ('channel.chatter', 'points', ('best',))."""
    for ns in _NS_LONGEST_FIRST:
        if token.startswith(ns + "."):
            name, *path = token[len(ns) + 1 :].split(".")
            if not name or (path and not allow_path):
                break
            return ns, name, tuple(path)
    raise CommandError(f"expected a variable like channel.deaths or chatter.location, got {token}")


def parse_value(raw: str) -> Any:
    """Numbers, booleans and JSON lists/objects are stored typed; everything else is text."""
    text = raw.strip()
    if text in ("true", "false"):
        return text == "true"
    if text[:1] in "[{" or text.lstrip("-").replace(".", "", 1).isdigit():
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return raw


def _key_for_user(ctx: CommandContext, ns: str, name: str, user_id: str) -> VarKey:
    column = CHATTER_KEY.get(ns)
    if column is None:
        raise CommandError(f"{ns} has no per-user values")
    return dataclasses.replace(key_for(ctx.exec, ns, name), **{column: user_id})


def _reject_filtered(ctx: CommandContext, text: str) -> None:
    """Stored text goes through the channel's filter too (architecture §9)."""
    filters = ctx.exec.services.get("filters")
    hits = filters.rejects(ctx.channel.id, text) if filters is not None else []
    if hits:
        raise CommandError(f"the filter rejects that: {', '.join(hits)}")


def _var_admin(ctx: CommandContext) -> bool:
    policy = ctx.exec.services.get("policy")
    return policy is not None and bool(policy.reaches_setting_role(ctx.exec, "var_admin_role"))


def _store(ctx: CommandContext) -> SqliteVariableStore:
    return ctx.service("variable_store")  # type: ignore[no-any-return]


@command(
    CommandSpec(
        name="var",
        module=MODULE,
        summary="Read and change variables",
        description=USAGE,
        params=(Param("1+", "arguments", description=USAGE),),
        examples=(
            Example("{sign}var set chatter.location Lisbon", "chatter.location = Lisbon"),
            Example("{sign}var incr channel.deaths", "channel.deaths = 13"),
            Example("{sign}var top channel.chatter.points", "1. alice 120, 2. bob 90"),
        ),
        default_cooldowns={"everyone": Cooldown(tier_s=0, user_s=3)},
        log_level=LogLevel.INVOCATIONS,
    )
)
async def var_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    return await _var(ctx, list(args.values))


async def _var(ctx: CommandContext, v: list[str]) -> Result:
    if not v:
        raise CommandError(f"usage: {USAGE}")
    action = v[0].lower()
    access = ctx.exec.variables.access

    if action == "list":
        if len(v) < 2 or v[1] not in VAR_NAMESPACES:
            raise CommandError("usage: var list <" + "|".join(VAR_NAMESPACES) + "> [user]")
        ns = v[1]
        probe = key_for(ctx.exec, ns, "probe")
        if len(v) > 2:
            user = await user_arg(ctx, v[2])
            probe = _key_for_user(ctx, ns, "probe", user["id"])
        entries = await _store(ctx).entries(Space(probe.ns, probe.key1, probe.key2, probe.key3))
        if not entries:
            return Result.success(f"no {ns} variables", {})
        return Result.success(
            ", ".join(f"{e.key.name}={render(e.value)}" for e in entries),
            {e.key.name: e.value for e in entries},
        )

    if len(v) < 2:
        raise CommandError(f"usage: {USAGE}")

    if action == "get":
        ns, name, path = parse_ref(v[1], allow_path=True)
        key = key_for(ctx.exec, ns, name)
        if len(v) > 2:
            key = _key_for_user(ctx, ns, name, (await user_arg(ctx, v[2]))["id"])
        value = descend(await ctx.variables.get(key), path)
        label = v[1] + (f" ({v[2]})" if len(v) > 2 else "")
        if value is MISSING:
            return Result.failure(Code.NOT_FOUND, f"{label} is not set")
        return Result.success(f"{label} = {render(value)}", value)

    if action == "top":
        ns, name, _ = parse_ref(v[1])
        if ns not in ("channel.chatter", "publisher.channel.chatter", "publisher.chatter"):
            raise CommandError(
                "top works on channel.chatter.*, publisher.chatter.* and publisher.channel.chatter.* values"
            )
        count = 5
        if len(v) > 2:
            if not v[2].isdigit() or not 1 <= int(v[2]) <= 25:
                raise CommandError("count must be 1–25")
            count = int(v[2])
        if ctx.invoker is None:
            raise CommandError("top needs a chatter")
        space_key = key_for(ctx.exec, ns, name)
        rows = await _store(ctx).top(ns, space_key.key1, space_key.key2, name, count)
        resolve_login = ctx.exec.services.get("login_for")
        ranked = []
        for i, (user_id, value) in enumerate(rows, start=1):
            login = (await resolve_login(user_id)) if resolve_login else None
            ranked.append((i, login or user_id, value))
        if not ranked:
            return Result.success(f"nobody has {v[1]} yet", [])
        text = ", ".join(f"{i}. {who} {render(val)}" for i, who, val in ranked)
        return Result.success(text, [{"rank": i, "user": who, "value": val} for i, who, val in ranked])

    ns, name, _ = parse_ref(v[1])

    if action in ("set", "incr"):
        if action == "set":
            _reject_filtered(ctx, " ".join(v[2:]))
        if not access.can_write(ctx.exec, ns, name):
            # Raised, not returned: a write denial is the runtime's 126, not a command's own failure code.
            raise CommandError(f"you can't change {ns}.{name}", Code.DENIED)
        key = key_for(ctx.exec, ns, name)
        if action == "set":
            if len(v) < 3:
                raise CommandError("usage: var set <ns.name> <value>")
            new = await ctx.variables.buffer(WriteOp("set", key, parse_value(" ".join(v[2:]))))
        else:
            amount: Any = 1
            if len(v) > 2:
                amount = parse_value(v[2])
                if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                    raise CommandError("amount must be a number")
            new = await ctx.variables.buffer(WriteOp("incr", key, amount))
        return Result.success(f"{ns}.{name} = {render(new)}", new)

    if action == "del":
        if len(v) > 2:
            user = await user_arg(ctx, v[2])
            key = _key_for_user(ctx, ns, name, user["id"])
            own = ctx.invoker is not None and user["id"] == ctx.invoker.id
            allowed = own and access.can_write(ctx.exec, ns, name)
            if not own:
                allowed = (ns == "channel.chatter" and _var_admin(ctx)) or rank(ctx) >= BOT_ADMIN_RANK
            label = f"{ns}.{name} for {user['display']}"
        else:
            key = key_for(ctx.exec, ns, name)
            allowed = access.can_write(ctx.exec, ns, name)
            label = f"{ns}.{name}"
        if not allowed:
            raise CommandError(f"you can't delete {label}", Code.DENIED)
        if await ctx.variables.get(key) is MISSING:
            return Result.failure(Code.NOT_FOUND, f"{label} is not set")
        await ctx.variables.buffer(WriteOp("delete", key))
        return Result.success(f"deleted {label}")

    raise CommandError(f"usage: {USAGE}")


COMMANDS: tuple[Command, ...] = (var_cmd,)
