"""Policy gate used by preflight: toggles, capabilities, permissions, cooldowns (ADR-0006).

The runtime depends only on this protocol; doomtp_bot.policy provides the real implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from doomtp_bot.runtime.result import Code

if TYPE_CHECKING:
    from doomtp_bot.runtime.context import ExecContext
    from doomtp_bot.runtime.spec import CommandSpec


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    code: int = Code.OK  # 126 denied, 127 disabled/unavailable, 128 cooldown
    reason: str = ""
    info: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls) -> Decision:
        return cls(True)


class Policy(Protocol):
    def check(self, ctx: ExecContext, spec: CommandSpec) -> Decision:
        """Preflight: toggles, capabilities, permission, cooldown — in that order."""

    def is_permitted(self, ctx: ExecContext, spec: CommandSpec) -> bool:
        """Toggles + capabilities + permission only (used for parse-error visibility, !help)."""

    def commit_cooldown(self, ctx: ExecContext, spec: CommandSpec) -> None:
        """Start both cooldown buckets for an invocation that is about to execute."""


class AllowAllPolicy:
    def check(self, ctx: ExecContext, spec: CommandSpec) -> Decision:
        return Decision.allow()

    def is_permitted(self, ctx: ExecContext, spec: CommandSpec) -> bool:
        return True

    def commit_cooldown(self, ctx: ExecContext, spec: CommandSpec) -> None:
        return None
