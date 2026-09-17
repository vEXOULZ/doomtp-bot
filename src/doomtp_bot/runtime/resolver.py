"""Name resolution (spec §5.1). Built-ins only for now; custom commands plug in via `Resolver`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from doomtp_bot.runtime.registry import Command, CommandRegistry

if TYPE_CHECKING:
    from doomtp_bot.lang.ast import Invocation
    from doomtp_bot.runtime.context import ExecContext


@dataclass(frozen=True, slots=True)
class Resolved:
    command: Command
    source: Literal["builtin", "publication", "personal"] = "builtin"


class Resolver(Protocol):
    def resolve(self, ctx: ExecContext, invocation: Invocation) -> Resolved | None: ...

    def resolve_name(self, ctx: ExecContext, name: str, personal: bool = False) -> Resolved | None: ...


class BuiltinResolver:
    def __init__(self, registry: CommandRegistry) -> None:
        self.registry = registry

    def resolve(self, ctx: ExecContext, invocation: Invocation) -> Resolved | None:
        return self.resolve_name(ctx, invocation.name, invocation.personal)

    def resolve_name(self, ctx: ExecContext, name: str, personal: bool = False) -> Resolved | None:
        if personal:
            return None  # personal aliases arrive with custom commands (ADR-0009)
        command = self.registry.get(name)
        return Resolved(command) if command is not None else None
