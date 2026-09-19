"""`help` module: lists only the commands the caller can run here (architecture §8, F9).

Custom commands appear alongside built-ins: what this channel publishes, plus the caller's own
aliases (ADR-0009). Each is checked against the same policy gate as a built-in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from doomtp_bot.customcmds import params as cc_params
from doomtp_bot.customcmds.packs import PackService
from doomtp_bot.customcmds.resolution import spec_for
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, CommandRegistry, command
from doomtp_bot.runtime.result import Code, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, Param, with_sign

if TYPE_CHECKING:
    from doomtp_bot.customcmds.service import CustomCommandService

MODULE = "help"
HIDDEN_MODULES = frozenset({"core"})


def _custom(ctx: CommandContext) -> CustomCommandService | None:
    service: CustomCommandService | None = ctx.exec.services.get("customcmds")
    return service


async def _custom_specs(ctx: CommandContext) -> dict[str, CommandSpec]:
    """Custom commands the caller could run here, by the name they'd type."""
    service = _custom(ctx)
    if service is None:
        return {}
    specs: dict[str, CommandSpec] = {}
    for scope in (ctx.channel.id, GLOBAL):  # channel publications, then derived commands (ADR-0012)
        for publication, command_ in await service.publications_in(scope):
            if publication.status == "active" and command_.status == "active":
                specs.setdefault(publication.name, spec_for(publication.name, command_, publication))
    packs: PackService | None = ctx.exec.services.get("packs")
    if packs is not None:
        for pack_publication, pack in await packs.publications_in(ctx.channel.id, include_global=True):
            if pack_publication.status != "active":
                continue
            for member in await packs.members(pack.id):
                specs.setdefault(member.name, spec_for(member.name, member, None, pack))
    if ctx.invoker is not None:
        for alias, command_ in await service.linked_by(ctx.invoker.id):
            specs.setdefault(alias, spec_for(alias, command_, None))
    return specs


@command(
    CommandSpec(
        name="help",
        module=MODULE,
        aliases=("commands",),
        summary="List commands you can use, or show how to use one",
        params=(Param("1", "command", description="A command name"),),
        examples=(
            Example("{sign}help", "commands: ping, random, …"),
            Example("{sign}help random", "{sign}random [range] — …"),
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
        wanted = name.lower().removeprefix(prefix).removeprefix("@")
        found = registry.get(wanted)
        spec = found.spec if found is not None else (await _custom_specs(ctx)).get(wanted)
        if spec is None or not policy.is_permitted(ctx.exec, spec):
            return Result.failure(Code.NOT_FOUND, f"no command named {name}")
        text = f"{prefix}{spec.usage()} — {with_sign(spec.summary, prefix)}"
        if spec.aliases:
            text += f" (aliases: {', '.join(spec.aliases)})"
        if spec.params:
            text += f" — {cc_params.describe(spec.params)}"
        return Result.success(text, {"name": spec.name, "usage": spec.usage(), "summary": spec.summary})

    builtins = [
        c.spec.name
        for c in registry.all()
        if c.spec.module not in HIDDEN_MODULES and policy.is_permitted(ctx.exec, c.spec)
    ]
    custom = sorted(
        n for n, spec in (await _custom_specs(ctx)).items() if policy.is_permitted(ctx.exec, spec)
    )
    text = "commands: " + ", ".join(builtins)
    if custom:
        text += " — custom: " + ", ".join(custom)
    return Result.success(text, {"builtin": builtins, "custom": custom})


COMMANDS: tuple[Command, ...] = (help_cmd,)
