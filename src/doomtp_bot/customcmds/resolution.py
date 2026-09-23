"""Turning stored custom commands into things the runtime can resolve (spec §5.1, ADR-0009).

The runtime's preflight is synchronous, so every custom command an expression could reach — including
the ones inside other bodies — is loaded and parsed *before* preflight, then handed over as a resolver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from doomtp_bot.customcmds.packs import Pack, PackService
from doomtp_bot.customcmds.params import to_params
from doomtp_bot.customcmds.service import CustomCommand, CustomCommandService, Publication
from doomtp_bot.lang.ast import Invocation, Node, invocations
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.runtime.preflight import MAX_CC_DEPTH
from doomtp_bot.runtime.resolver import CustomTarget, Resolved, Resolver, Source
from doomtp_bot.runtime.spec import CommandSpec, InputMode, LogLevel, Param

if TYPE_CHECKING:
    from doomtp_bot.runtime.context import ExecContext

log = structlog.get_logger(__name__)

MODULE = "custom"


def spec_for(
    name: str, command: CustomCommand, publication: Publication | None, pack: Pack | None = None
) -> CommandSpec:
    """A CommandSpec for a custom command. `policy_key` is the command id, so permissions and cooldowns
    follow the command rather than the name it happens to be published under.

    A command reached through a pack takes the pack's name as its module, so `!module disable <pack>`
    turns the whole set off in a channel (ADR-0012).
    """
    summary = command.summary or f"custom command by @{command.owner_login}"
    return CommandSpec(
        name=name,
        module=pack.name if pack is not None else MODULE,
        summary=summary,
        description=command.body,
        params=to_params(command.params)
        or (Param("1+", "arguments", required=False, description="passed to the command body"),),
        input=InputMode.OPTIONAL,
        required_role=(publication.required_role if publication else None) or "everyone",
        log_level=LogLevel.INVOCATIONS,
        policy_key=command.id,
    )


@dataclass(frozen=True, slots=True)
class _Key:
    name: str
    personal: bool


class PreloadedResolver:
    """Built-in first, then whatever the loader found for this expression (spec §5.1)."""

    def __init__(self, base: Resolver, entries: dict[_Key, Resolved]) -> None:
        self.base = base
        self.entries = entries

    def resolve(self, ctx: ExecContext, invocation: Invocation) -> Resolved | None:
        return self.resolve_name(ctx, invocation.name, invocation.personal)

    def resolve_name(self, ctx: ExecContext, name: str, personal: bool = False) -> Resolved | None:
        if not personal:
            found = self.base.resolve_name(ctx, name)
            if found is not None:
                return found
        return self.entries.get(_Key(name, personal))


class CustomCommandLoader:
    """Loads and parses every custom command an expression can reach, then builds a resolver."""

    def __init__(
        self,
        service: CustomCommandService,
        packs: PackService | None = None,
    ) -> None:
        self.service = service
        self.packs = packs

    async def resolver_for(self, ctx: ExecContext, node: Node, base: Resolver) -> Resolver:
        entries: dict[_Key, Resolved] = {}
        pending = self._names(node)
        seen: set[_Key] = set()
        for _ in range(MAX_CC_DEPTH + 1):
            wanted = [key for key in pending if key not in seen]
            if not wanted:
                break
            pending = []
            for key in wanted:
                seen.add(key)
                if not key.personal and base.resolve_name(ctx, key.name) is not None:
                    continue  # a built-in of that name wins (spec §5.1)
                resolved = await self._lookup(ctx, key)
                if resolved is None:
                    continue
                entries[key] = resolved
                assert resolved.custom is not None
                pending.extend(self._names(resolved.custom.body))
        return PreloadedResolver(base, entries) if entries else base

    @staticmethod
    def _names(node: Node) -> list[_Key]:
        return [_Key(inv.name, inv.personal) for inv in invocations(node)]

    async def _lookup(self, ctx: ExecContext, key: _Key) -> Resolved | None:
        publication: Publication | None = None
        pack: Pack | None = None
        source: Source = "personal"
        command: CustomCommand | None = None
        if key.personal:  # `@name` addresses the invoker's own alias, skipping every publication
            command = await self._personal(ctx, key.name)
        else:
            found = await self.service.publication_in_scope(ctx.channel.id, key.name)
            if found is not None:
                publication, command, source = found[0], found[1], "publication"
            elif self.packs is not None:
                in_pack = await self.packs.find_in_scope(ctx.channel.id, key.name)
                if in_pack is not None:
                    command, pack, source = in_pack[0], in_pack[1], "publication"
            if command is None:
                command = await self._personal(ctx, key.name)
        if command is None:
            return None
        try:
            body = self.service.ast_for(command, ctx.channel.prefix)
        except (ParseError, Exception) as exc:  # a body saved by an older syntax version
            log.warning("customcmd.body_unparsable", command_id=command.id, error=repr(exc))
            return None
        target = CustomTarget(
            command_id=command.id,
            owner_id=command.owner_user_id,
            owner_login=command.owner_login,
            name=command.name,
            version=command.version,
            body=body,
            publication=publication.name if publication else (command.name if pack else None),
        )
        return Resolved(spec_for(key.name, command, publication, pack), custom=target, source=source)

    async def _personal(self, ctx: ExecContext, alias: str) -> CustomCommand | None:
        if ctx.invoker is None:
            return None
        return await self.service.personal(ctx.invoker.id, alias)
