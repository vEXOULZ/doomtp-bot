"""Helpers shared by built-in command handlers."""

from __future__ import annotations

from typing import Any

from doomtp_bot.policy.repository import Actor
from doomtp_bot.runtime.context import CommandContext
from doomtp_bot.runtime.registry import CommandRegistry
from doomtp_bot.runtime.result import CommandError
from doomtp_bot.runtime.spec import CommandSpec
from doomtp_bot.runtime.values import ConversionError, convert


def rank(ctx: CommandContext) -> int:
    return ctx.invoker.rank if ctx.invoker else 0


def actor(ctx: CommandContext) -> Actor:
    return Actor(ctx.invoker.id if ctx.invoker else None, "chat")


async def user_arg(ctx: CommandContext, raw: str) -> dict[str, Any]:
    """Resolve a user argument (`@login` or login) or fail the command with a usage error."""
    try:
        return await convert(raw, "user", resolve_user=ctx.exec.resolve_user)  # type: ignore[no-any-return]
    except ConversionError as exc:
        raise CommandError(str(exc)) from exc


def command_spec(ctx: CommandContext, name: str) -> CommandSpec:
    """Look up a command by name or alias, with or without the channel prefix."""
    registry: CommandRegistry = ctx.service("registry")
    found = registry.get(name.lower().removeprefix(ctx.channel.prefix))
    if found is None:
        raise CommandError(f"unknown command: {name}")
    return found.spec
