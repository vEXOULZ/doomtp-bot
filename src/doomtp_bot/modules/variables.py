"""`variables` module: !var — read, write and rank variables (ADR-0010, variable-access-matrix.md §3, §5).

!var acts as the typed expression: its writes follow the Typed column and go through the run's write buffer,
so they commit atomically with the rest of the line.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import TYPE_CHECKING, Any

from doomtp_bot.lang.ast import Lit
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import parse_var_ref
from doomtp_bot.modules._common import rank, reject_filtered, user_arg
from doomtp_bot.policy.roles import BOT_ADMIN_RANK
from doomtp_bot.runtime import ops
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.executor import store_key
from doomtp_bot.runtime.namespaces import CHATTER_KEY, VAR_NAMESPACES
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Code, CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, LogLevel, Param
from doomtp_bot.runtime.values import MISSING, render
from doomtp_bot.runtime.variables import (
    ANY,
    Space,
    VarKey,
    WriteOp,
    format_size,
    key_for,
    owner_of,
    pop_item,
)

if TYPE_CHECKING:
    from doomtp_bot.variables.store import PostgresVariableStore

MODULE = "variables"
USAGE = (
    "var get <ns.name[path]> [user] | set <ns.name[path]> <value> | incr <ns.name[path]> [amount]"
    " | del <ns.name[path]> [user] | pop <ns.name[path]> [index] | list <ns> [user] | top <ns.name> [count]"
    " | usage [ns]"
)


def parse_ref(token: str, *, allow_path: bool = False) -> tuple[str, str, tuple[str | int, ...]]:
    """'channel.chatter.points[best][0]' → ('channel.chatter', 'points', ('best', 0)) (ADR-0018).

    The typed line already put any `{…}` into the text, so a path step is a plain key or index here.
    """
    try:
        ref = parse_var_ref(token)
    except ParseError:
        raise CommandError(f"expected a variable like channel.deaths or channel.stats[kills], got {token}") from None
    if ref.path and not allow_path:
        raise CommandError(f"{ref.namespace}.{ref.name} takes no [path] here")
    steps: list[str | int] = []
    for step in ref.path:
        if not isinstance(step, Lit):
            raise CommandError("a path takes plain keys and numbers here; put a value in with {…}")
        steps.append(store_key(step.value))
    return ref.namespace, ref.name, tuple(steps)


def _label(ns: str, name: str, path: tuple[str | int, ...]) -> str:
    return f"{ns}.{name}" + "".join(f"[{step}]" for step in path)


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


def _var_admin(ctx: CommandContext) -> bool:
    policy = ctx.exec.services.get("policy")
    return policy is not None and bool(policy.reaches_setting_role(ctx.exec, "var_admin_role"))


def _store(ctx: CommandContext) -> PostgresVariableStore:
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
        # The documented exception (variable-access-matrix.md §2): the variable is its argument.
        reads=(ANY,),
        writes=(ANY,),
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

    if action == "usage":
        return await _usage(ctx, v[1] if len(v) > 1 else "chatter")

    if len(v) < 2:
        raise CommandError(f"usage: {USAGE}")

    if action == "get":
        ns, name, path = parse_ref(v[1], allow_path=True)
        key = key_for(ctx.exec, ns, name)
        if len(v) > 2:
            key = _key_for_user(ctx, ns, name, (await user_arg(ctx, v[2]))["id"])
        value = await ctx.variables.get(key)
        for step in path:
            value = ops.index(value, step)
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
        # All at once: a cold cache is a Helix call per row, and one after another a long board would
        # run past the command's stage timeout.
        logins = await asyncio.gather(*(resolve_login(uid) for uid, _ in rows)) if resolve_login else []
        ranked = [
            (i, (logins[i - 1] if logins else None) or user_id, value)
            for i, (user_id, value) in enumerate(rows, start=1)
        ]
        if not ranked:
            return Result.success(f"nobody has {v[1]} yet", [])
        text = ", ".join(f"{i}. {who} {render(val)}" for i, who, val in ranked)
        return Result.success(text, [{"rank": i, "user": who, "value": val} for i, who, val in ranked])

    ns, name, path = parse_ref(v[1], allow_path=True)
    label = _label(ns, name, path)

    if action in ("set", "incr"):
        if action == "set":
            reject_filtered(ctx, " ".join(v[2:]))
        if not access.can_write(ctx.exec, ns, name):
            # Raised, not returned: a write denial is the runtime's 126, not a command's own failure code.
            raise CommandError(f"you can't change {ns}.{name}", Code.DENIED)
        key = key_for(ctx.exec, ns, name)
        if action == "set":
            if len(v) < 3:
                raise CommandError("usage: var set <ns.name[path]> <value>")
            op = WriteOp("set", key, parse_value(" ".join(v[2:])), path)
        else:
            amount: Any = 1
            if len(v) > 2:
                amount = parse_value(v[2])
                if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                    raise CommandError("amount must be a number")
            op = WriteOp("incr", key, amount, path)
        new = await ctx.variables.buffer(op)
        for step in path:
            new = ops.index(new, step)
        return Result.success(f"{label} = {render(new)}", new)

    if action == "pop":
        if not access.can_write(ctx.exec, ns, name):
            raise CommandError(f"you can't change {ns}.{name}", Code.DENIED)
        index: int | None = None
        if len(v) > 2:
            try:
                index = int(v[2])
            except ValueError:
                raise CommandError("index must be a whole number, e.g. 0 or -1") from None
        op = WriteOp("pop", key_for(ctx.exec, ns, name), index, path)
        item = pop_item(await ctx.variables.get(op.key), op)
        await ctx.variables.buffer(op)
        return Result.success(render(item), item)

    if action == "del":
        if len(v) > 2:
            user = await user_arg(ctx, v[2])
            key = _key_for_user(ctx, ns, name, user["id"])
            own = ctx.invoker is not None and user["id"] == ctx.invoker.id
            allowed = own and access.can_write(ctx.exec, ns, name)
            if not own:
                allowed = (ns == "channel.chatter" and _var_admin(ctx)) or rank(ctx) >= BOT_ADMIN_RANK
            label = f"{label} for {user['display']}"
        else:
            key = key_for(ctx.exec, ns, name)
            allowed = access.can_write(ctx.exec, ns, name)
        if not allowed:
            raise CommandError(f"you can't delete {label}", Code.DENIED)
        if await ctx.variables.get(key) is MISSING:
            return Result.failure(Code.NOT_FOUND, f"{label} is not set")
        # With a path, a key or item that isn't there is E_KEY / E_INDEX (ADR-0019 D4c).
        await ctx.variables.buffer(WriteOp("delete", key, path=path))
        return Result.success(f"deleted {label}")

    raise CommandError(f"usage: {USAGE}")


async def _usage(ctx: CommandContext, ns: str) -> Result:
    """How much of its owner's quota a namespace's owner uses: `channel` and `channel.chatter` both count
    against the channel (ADR-0019)."""
    if ns not in VAR_NAMESPACES:
        raise CommandError("usage: var usage [" + "|".join(VAR_NAMESPACES) + "]")
    kind, owner_id = owner_of(key_for(ctx.exec, ns, "probe"))
    store = _store(ctx)
    used = await store.usage(kind, owner_id)
    quota = (await store.limits_for(kind, owner_id)).quota_bytes
    total = sum(used.values())
    percent = f" ({total * 100 // quota}%)" if quota else ""
    detail = ", ".join(f"{name} {format_size(size)}" for name, size in sorted(used.items()))
    return Result.success(
        f"{kind} storage: {format_size(total)} of {format_size(quota)}{percent}"
        + (f"; {detail}" if len(used) > 1 else ""),
        {"owner": kind, "used": total, "quota": quota, "namespaces": used},
    )


COMMANDS: tuple[Command, ...] = (var_cmd,)
