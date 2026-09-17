"""Toggleable command groups. core (sentinels: true, false, default, fail, echo) and core_admin are not toggleable."""

from __future__ import annotations

from doomtp_bot.runtime.registry import CommandRegistry

NON_TOGGLEABLE_MODULES = frozenset({"core", "core_admin"})


def builtin_registry() -> CommandRegistry:
    from doomtp_bot.modules import basic, core, core_admin, help, variables

    registry = CommandRegistry()
    for module in (core, core_admin, help, basic, variables):
        registry.extend(module.COMMANDS)
    return registry
