"""`help` module: `commands` and `help` list only the commands the caller can run here (architecture §8, F9).
When WEB_SITE_URL is set, `commands` links the channel's command page on the web site and `help` the site
itself, where the language and the docs are.

Custom commands appear alongside built-ins: what this channel publishes, plus the caller's own
aliases (ADR-0009). Each is checked against the same policy gate as a built-in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from doomtp_bot.customcmds import params as cc_params
from doomtp_bot.customcmds.packs import PackService
from doomtp_bot.customcmds.resolution import spec_for
from doomtp_bot.lang.parser import strip_prefix
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, CommandRegistry, command
from doomtp_bot.runtime.result import Code, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, Param, with_sign

if TYPE_CHECKING:
    from doomtp_bot.customcmds.service import CustomCommandService

MODULE = "help"
HIDDEN_MODULES = frozenset({"core", "http"})  # http: only admins' commands can call it


def site_root(ctx: CommandContext) -> str | None:
    """The web site's home, with the docs and the language."""
    site: str | None = ctx.exec.services.get("site_url")
    return f"{site.rstrip('/')}/" if site else None


def site_link(ctx: CommandContext) -> str | None:
    """The channel's page on the web site, which lists every command with its usage."""
    root = site_root(ctx)
    return f"{root}channels/{ctx.channel.login}" if root else None


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
            for member in await packs.members(pack.id, internal=False):  # helpers aren't typed
                specs.setdefault(member.name, spec_for(member.name, member, None, pack))
    if ctx.invoker is not None:
        for alias, command_ in await service.linked_by(ctx.invoker.id):
            specs.setdefault(alias, spec_for(alias, command_, None))
    return specs


async def _listing(ctx: CommandContext, link: str | None, how: str) -> Result:
    """`commands: …; custom: …`, the ones the caller can run here, then `; <how> <link>` when the site is set."""
    registry: CommandRegistry = ctx.service("registry")
    policy = ctx.service("runtime").policy
    builtins = [
        c.spec.name
        for c in registry.all()
        if c.spec.module not in HIDDEN_MODULES and policy.is_permitted(ctx.exec, c.spec)
    ]
    custom = sorted(n for n, spec in (await _custom_specs(ctx)).items() if policy.is_permitted(ctx.exec, spec))
    text = "commands: " + ", ".join(builtins)
    if custom:
        text += "; custom: " + ", ".join(custom)
    if link:
        text += f"; {how} {link}"
    return Result.success(text, {"builtin": builtins, "custom": custom, "link": link})


@command(
    CommandSpec(
        name="help",
        module=MODULE,
        summary="List commands you can use, or show how to use one, with a link to the bot's docs",
        params=(Param("1", "command", description="A command name"),),
        examples=(
            Example("{sign}help", "commands: explain, help, random, …; docs at …"),
            Example("{sign}help random", "{sign}random [range]: …"),
        ),
        default_cooldowns={"everyone": Cooldown(tier_s=5, user_s=15)},
    )
)
async def help_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    policy = ctx.service("runtime").policy
    prefix = ctx.channel.prefix
    name = args.get("command")
    if not name:
        return await _listing(ctx, site_root(ctx), "docs at")
    wanted = strip_prefix(name, prefix).lower().removeprefix("@")
    # The runtime's own resolver: the built-ins and the system packs' sentinels (ADR-0019).
    found = ctx.service("runtime").resolver.resolve_name(ctx.exec, wanted)
    spec = found.spec if found is not None else (await _custom_specs(ctx)).get(wanted)
    if spec is None or not policy.is_permitted(ctx.exec, spec):
        return Result.failure(Code.NOT_FOUND, f"no command named {name}")
    text = f"{prefix}{spec.usage()}: {with_sign(spec.summary, prefix)}"
    if spec.aliases:
        text += f" (aliases: {', '.join(spec.aliases)})"
    if spec.params:
        text += f". {cc_params.describe(spec.params)}"
    return Result.success(text, {"name": spec.name, "usage": spec.usage(), "summary": spec.summary})


@command(
    CommandSpec(
        name="commands",
        module=MODULE,
        summary="List commands you can use here, with a link to this channel's command page",
        examples=(Example("{sign}commands", "commands: explain, help, random, …; more at …/channels/<channel>"),),
        default_cooldowns={"everyone": Cooldown(tier_s=5, user_s=15)},
    )
)
async def commands_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    return await _listing(ctx, site_link(ctx), "more at")


COMMANDS: tuple[Command, ...] = (help_cmd, commands_cmd)
