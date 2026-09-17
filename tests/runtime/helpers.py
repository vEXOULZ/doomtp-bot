"""Test fixtures for the runtime: a registry with fake commands and a context factory."""

from __future__ import annotations

import asyncio
import random
from typing import Any

from doomtp_bot.lang.parser import Context
from doomtp_bot.modules import builtin_registry
from doomtp_bot.runtime.context import Args, ChannelInfo, Chatter, CommandContext
from doomtp_bot.runtime.engine import RunReport, Runtime
from doomtp_bot.runtime.registry import Command, CommandRegistry, command
from doomtp_bot.runtime.result import Code, Result
from doomtp_bot.runtime.spec import CommandSpec, InputMode, Param

CHANNEL = ChannelInfo(id="c1", login="doomtp", display="DoomTP", prefix="!")
ALICE = Chatter(
    id="u1",
    login="alice",
    display="Alice",
    badges=frozenset({"subscriber"}),
    roles=("everyone", "subscriber"),
    rank=20,
)


@command(
    CommandSpec(
        name="weather",
        module="test",
        summary="fake weather",
        params=(Param("1+", "location", required=True),),
        input=InputMode.NONE,
    )
)
async def weather(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    location = args["location"]
    if location.lower() == "nowhere":
        return Result.failure(Code.NOT_FOUND, "location not found")
    return Result.success(
        f"{location}: 21.5°C", {"celsius": 21.5, "location": location, "tags": ["sun", "warm"]}
    )


@command(CommandSpec(name="upper", module="test", summary="uppercase stdin", input=InputMode.REQUIRED))
async def upper(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    text = (stdin.message if stdin else None) or ""
    return Result.success(text.upper(), text.upper())


@command(CommandSpec(name="slow", module="test", summary="sleeps"))
async def slow(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    await asyncio.sleep(10)
    return Result.success("done")


@command(CommandSpec(name="boom", module="test", summary="raises"))
async def boom(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    raise RuntimeError("kaboom")


@command(
    CommandSpec(
        name="add",
        module="test",
        summary="adds ints",
        params=(Param("1", "a", type="int", required=True), Param("2", "b", type="int", required=True)),
    )
)
async def add(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    total = args["a"] + args["b"]
    return Result.success(str(total), total)


@command(CommandSpec(name="explain", module="test", summary="raw tail echo", raw_tail_from=1))
async def explain(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    return Result.success(f"raw={args.raw_tail}")


@command(CommandSpec(name="cancelme", module="test", summary="checks cancellation", side_effects=True))
async def cancelme(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    ctx.ensure_not_cancelled()
    return Result.success("not cancelled")


@command(CommandSpec(name="fakedeny", module="test", summary="returns a reserved code"))
async def fakedeny(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    return Result.failure(Code.DENIED, "nope")


TEST_COMMANDS: tuple[Command, ...] = (weather, upper, slow, boom, add, explain, cancelme, fakedeny)


def registry() -> CommandRegistry:
    reg = builtin_registry()
    reg.extend(TEST_COMMANDS)
    return reg


def make_runtime(**kwargs: Any) -> Runtime:
    return Runtime(registry(), **kwargs)


async def run(
    runtime: Runtime,
    text: str,
    *,
    invoker: Chatter | None = ALICE,
    context: Context = Context.LINE,
    channel: ChannelInfo = CHANNEL,
    seed: int = 7,
    **kwargs: Any,
) -> RunReport:
    run_kwargs = {
        k: kwargs.pop(k) for k in ("reply_parent_login", "scope_args", "publisher", "stdin") if k in kwargs
    }
    ctx = runtime.make_context(
        channel=channel, invoker=invoker, context=context, rng=random.Random(seed), **kwargs
    )
    report = await runtime.run(text, ctx, **run_kwargs)
    assert report is not None, f"{text!r} was not a command"
    return report
