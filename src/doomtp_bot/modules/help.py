"""`help` module: lists only the commands the caller can run here (architecture §8, F9)."""

from __future__ import annotations

from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, CommandRegistry, command
from doomtp_bot.runtime.result import Code, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, Param

MODULE = "help"
HIDDEN_MODULES = frozenset({"core"})


@command(
    CommandSpec(
        name="help",
        module=MODULE,
        aliases=("commands",),
        summary="List commands you can use, or show how to use one",
        params=(Param("1", "command", description="A command name"),),
        examples=(
            Example("!help", "commands: ping, random, …"),
            Example("!help random", "!random [range] — …"),
        ),
        default_cooldowns={"everyone": Cooldown(tier_s=5, user_s=15)},
    )
)
async def help_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    registry: CommandRegistry = ctx.service("registry")
    policy = ctx.service("runtime").policy
    prefix = ctx.channel.prefix
    name = args.get("command")
    if name:
        found = registry.get(name.lower().removeprefix(prefix))
        if found is None or not policy.is_permitted(ctx.exec, found.spec):
            return Result.failure(Code.NOT_FOUND, f"no command named {name}")
        spec = found.spec
        text = f"{prefix}{spec.usage()} — {spec.summary}"
        if spec.aliases:
            text += f" (aliases: {', '.join(spec.aliases)})"
        return Result.success(text, {"name": spec.name, "usage": spec.usage(), "summary": spec.summary})
    names = [
        c.spec.name
        for c in registry.all()
        if c.spec.module not in HIDDEN_MODULES and policy.is_permitted(ctx.exec, c.spec)
    ]
    return Result.success("commands: " + ", ".join(names), names)


COMMANDS: tuple[Command, ...] = (help_cmd,)
