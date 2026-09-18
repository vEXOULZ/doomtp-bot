"""Name resolution (spec §5.1): built-in, then channel publication, then the invoker's personal alias.

The runtime never touches storage. A custom command reaches it as a `CustomTarget`: an already-parsed
body plus who owns it, which `customcmds/` builds from the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from doomtp_bot.runtime.registry import CommandRegistry, Handler
from doomtp_bot.runtime.spec import CommandSpec

if TYPE_CHECKING:
    from doomtp_bot.lang.ast import Invocation, Node
    from doomtp_bot.runtime.context import ExecContext

Source = Literal["builtin", "publication", "personal"]


@dataclass(frozen=True, slots=True)
class CustomTarget:
    """A custom command ready to run: its body AST and the identity it runs on behalf of (ADR-0009)."""

    command_id: str
    owner_id: str
    owner_login: str
    name: str  # the owner's canonical name
    version: int
    body: Node
    publication: str | None = None  # set when reached through a channel publication, not a personal link


@dataclass(frozen=True, slots=True)
class Resolved:
    spec: CommandSpec
    handler: Handler | None = None  # built-ins run a handler…
    custom: CustomTarget | None = None  # …custom commands run a body
    source: Source = "builtin"


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
            return None  # `@name` addresses a personal alias, which only customcmds/ can resolve
        command = self.registry.get(name)
        return Resolved(command.spec, command.handler) if command is not None else None
