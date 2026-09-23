"""Built-in command groups. Whether a group can be turned off is declared by its specs (CommandSpec.toggleable)."""

from __future__ import annotations

from doomtp_bot.runtime.registry import CommandRegistry


def builtin_registry() -> CommandRegistry:
    from doomtp_bot.modules import (
        automod,
        basic,
        channels,
        core,
        core_admin,
        customcmds,
        explain,
        filters,
        help,
        moderation,
        triggers,
        variables,
    )

    registry = CommandRegistry()
    for module in (
        core,
        core_admin,
        channels,
        help,
        basic,
        variables,
        customcmds,
        filters,
        automod,
        moderation,
        triggers,
        explain,
    ):
        registry.extend(module.COMMANDS)
    return registry
