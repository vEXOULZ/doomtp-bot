"""`core` module: sentinel commands (spec §8). Not toggleable, no cooldowns, logged at level `off`."""

from __future__ import annotations

from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Code, Result
from doomtp_bot.runtime.spec import CommandSpec, Example, InputMode, LogLevel, Param

MODULE = "core"


def _spec(**kwargs: object) -> CommandSpec:
    return CommandSpec(module=MODULE, log_level=LogLevel.OFF, input=InputMode.OPTIONAL, **kwargs)  # type: ignore[arg-type]


@command(
    _spec(
        name="true",
        summary="Always succeeds",
        description="Succeeds without a message and passes the previous data through. `!x || true` makes x optional.",
        examples=(Example("!shoutout @someone || true", "(nothing if the shoutout fails)"),),
    )
)
async def true_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    return Result.success(None, ctx.prev.data if ctx.prev is not None else None)


@command(_spec(name="false", summary="Always fails", description="Fails with code 1 and no message."))
async def false_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    return Result.failure(Code.FAIL)


@command(
    _spec(
        name="default",
        summary="Produce a fallback value",
        params=(Param("1+", "value", required=True, description="The value to produce"),),
        examples=(Example("( !weather x || default unknown ) > chatter.w", "unknown"),),
    )
)
async def default_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    value = args["value"]
    return Result.success(value, value)


@command(
    _spec(
        name="fail",
        summary="Fail with a code and message",
        params=(Param("1+", "message", description="Optional exit code (1–99) followed by a message"),),
        examples=(Example("!check {arg.1:int} <= 20 || fail 2 max 20 dice", "max 20 dice"),),
    )
)
async def fail_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    values = list(args.values)
    code: int = Code.FAIL
    if values and values[0].lstrip("+").isdigit() and 1 <= int(values[0]) <= 99:
        code = int(values.pop(0))
    message = " ".join(values) or None
    return Result.failure(code, message)


@command(
    _spec(
        name="echo",
        summary="Say something",
        params=(Param("1+", "text", description="Text to say"),),
        examples=(Example("!random 1-100 | echo you rolled {1}", "you rolled 42"),),
    )
)
async def echo_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    text = args.get("text", "")
    return Result.success(text, text)


COMMANDS: tuple[Command, ...] = (true_cmd, false_cmd, default_cmd, fail_cmd, echo_cmd)
SENTINEL_NAMES = frozenset(c.spec.name for c in COMMANDS)
