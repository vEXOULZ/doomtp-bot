"""Toggleable command groups. core (sentinels: true, false, default, fail, echo) and core_admin are not toggleable."""

from __future__ import annotations

from doomtp_bot.runtime.registry import CommandRegistry

NON_TOGGLEABLE_MODULES = frozenset({"core", "core_admin"})


def builtin_registry() -> CommandRegistry:
    from doomtp_bot.modules import basic, core

    registry = CommandRegistry()
    registry.extend(core.COMMANDS)
    registry.extend(basic.COMMANDS)
    return registry
