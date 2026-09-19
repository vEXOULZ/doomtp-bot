"""`explain` command: a dry run in words (spec §9).

`explain` takes the rest of the line raw, so the expression it reports on is exactly what you typed.
`--run` evaluates it too, with variable writes discarded, cooldowns untouched and nothing sent to chat.
"""

from __future__ import annotations

from doomtp_bot.lang.parser import Context
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.explain import explain
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, InputMode, LogLevel, Param

MODULE = "help"
USAGE = "explain [--run] [--as-body] <expression>"
FLAGS = {"--run", "--as-body"}


@command(
    CommandSpec(
        name="explain",
        module=MODULE,
        summary="Show what an expression would do, and why",
        description=USAGE,
        params=(Param("1+", "expression", description="The expression to explain"),),
        input=InputMode.OPTIONAL,
        raw_tail_from=1,
        default_cooldowns={"everyone": Cooldown(tier_s=0, user_s=10)},
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example(
                "{sign}explain {sign}random 1-6 | echo you rolled {1}",
                "Pipe(random,echo) — 1:random ✓, 2:echo ✓",
            ),
            Example("{sign}explain --run {sign}ping", "ping[] — 1:ping ✓ — ran: code 0, would send: pong"),
        ),
    )
)
async def explain_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    raw = (args.raw_tail or args.get("expression", "")).strip()
    flags: set[str] = set()
    while raw.split(" ", 1)[0] in FLAGS:
        flag, _, raw = raw.partition(" ")
        flags.add(flag)
        raw = raw.strip()
    if not raw:
        raise CommandError(f"usage: {ctx.channel.prefix}{USAGE}")

    report = await explain(
        ctx.service("runtime"),
        raw,
        ctx.exec,
        context=Context.BODY if "--as-body" in flags else Context.LINE,
        run="--run" in flags,
    )
    return Result.success(report.one_line(), report.as_dict())


COMMANDS: tuple[Command, ...] = (explain_cmd,)
