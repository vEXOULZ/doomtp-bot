"""Policy gate: toggles, capabilities and permissions in preflight, cooldowns at runtime (ADR-0006).

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
        """Preflight: toggles, capabilities, permission — in that order.

        Not cooldowns: since spec 1.1 those are the individual invocation's runtime failure, so `||` can
        handle them and a branch that never runs never trips one. See `claim_cooldown`.
        """

    def is_permitted(self, ctx: ExecContext, spec: CommandSpec) -> bool:
        """Toggles + capabilities + permission only (used for parse-error visibility, !help)."""

    def claim_cooldown(self, ctx: ExecContext, spec: CommandSpec, *, commit: bool = True) -> Decision:
        """Runtime (spec §6.3): refuse with 128 if either bucket is still running, otherwise start both.

        Checking and starting are one synchronous step, so two runs racing for a shared bucket can't both
        get through. With `commit=False` it only looks — a dry run (spec §9), or the early check the
        executor makes before expanding arguments.
        """


class AllowAllPolicy:
    def check(self, ctx: ExecContext, spec: CommandSpec) -> Decision:
        return Decision.allow()

    def is_permitted(self, ctx: ExecContext, spec: CommandSpec) -> bool:
        return True

    def claim_cooldown(self, ctx: ExecContext, spec: CommandSpec, *, commit: bool = True) -> Decision:
        return Decision.allow()
