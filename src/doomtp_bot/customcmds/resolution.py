"""Turning stored custom commands into things the runtime can resolve (spec §5.1, ADR-0009).

The runtime's preflight is synchronous, so every custom command an expression could reach — including
the ones inside other bodies — is loaded and parsed *before* preflight, then handed over as a resolver.

System packs (ADR-0019) are the exception: their members are sentinels, loaded once at startup into a
`SystemResolver`, which the runtime uses as its base resolver.
"""

from __future__ import annotations

from collections.abc import Iterable
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
    from doomtp_bot.customcmds.system import Derived
    from doomtp_bot.runtime.context import ExecContext

log = structlog.get_logger(__name__)

MODULE = "custom"


def spec_for(
    name: str, command: CustomCommand, publication: Publication | None, pack: Pack | None = None
) -> CommandSpec:
    """A CommandSpec for a custom command. `policy_key` is the command id, so permissions and cooldowns
    follow the command rather than the name it happens to be published under.

    A command reached through a pack takes the pack's name as its module, so `!module disable <pack>`
    turns the whole set off in a channel (ADR-0012). A system pack's members are sentinels instead: never
    switched off, role everyone, no cooldowns, not logged (spec §8, ADR-0019).
    """
    summary = command.summary or f"custom command by @{command.owner_login}"
    params = to_params(command.params) or (
        Param("1+", "arguments", required=False, description="passed to the command body"),
    )
    if pack is not None and pack.is_system:
        return CommandSpec(
            name=name,
            module=pack.name,
            summary=summary,
            description=command.body,
            params=params,
            input=InputMode.OPTIONAL,
            log_level=LogLevel.OFF,
            policy_key=command.id,
            toggleable=False,
            fixed_policy=True,
        )
    return CommandSpec(
        name=name,
        module=pack.name if pack is not None else MODULE,
        summary=summary,
        description=command.body,
        params=params,
        input=InputMode.OPTIONAL,
        required_role=(publication.required_role if publication else None) or "everyone",
        log_level=LogLevel.INVOCATIONS,
        policy_key=command.id,
    )


def _target(command: CustomCommand, body: Node, publication: str | None, pack: Pack | None) -> CustomTarget:
    return CustomTarget(
        command_id=command.id,
        owner_id=command.owner_user_id,
        owner_login=command.owner_login,
        name=command.name,
        version=command.version,
        body=body,
        publication=publication,
        pack_id=pack.id if pack is not None else None,
        system=pack is not None and pack.is_system,
    )


def _pack_id_of(ctx: ExecContext) -> str | None:
    """The pack whose command body is running, whose internal members it may call."""
    return ctx.publisher.pack_id if ctx.publisher is not None else None


class SystemResolver:
    """The system packs' members, then the built-ins (ADR-0019). The members are sentinels, so they
    resolve ahead of every other name, in every channel, without being published. An internal member
    resolves only inside the body of a command from its own pack."""

    def __init__(self, base: Resolver, members: Iterable[tuple[CustomCommand, Pack, bool]] = ()) -> None:
        self.base = base
        self.public: dict[str, tuple[CustomCommand, Pack]] = {}
        self.internal: dict[tuple[str, str], tuple[CustomCommand, Pack]] = {}
        self._asts: dict[tuple[str, int, str], Node] = {}
        self.replace(members)

    def replace(self, members: Iterable[tuple[CustomCommand, Pack, bool]]) -> None:
        public: dict[str, tuple[CustomCommand, Pack]] = {}
        internal: dict[tuple[str, str], tuple[CustomCommand, Pack]] = {}
        for command, pack, is_internal in members:
            if is_internal:
                internal[(pack.id, command.name)] = (command, pack)
            else:
                public[command.name] = (command, pack)
        self.public, self.internal = public, internal

    @classmethod
    async def load(cls, base: Resolver, packs: PackService) -> SystemResolver:
        return cls(base, await packs.system_members())

    async def reload(self, packs: PackService) -> None:
        """Read the system packs again, after the pack script changed them."""
        self.replace(await packs.system_members())

    @classmethod
    def from_derived(
        cls, base: Resolver, pack_name: str, derived: Iterable[Derived], *, version: int = 1
    ) -> SystemResolver:
        """A system pack straight from its definitions, with no database: tests and dry runs."""
        pack = Pack(f"system:{pack_name}", "bot", pack_name, "", "active", version)
        members = [
            (
                CustomCommand(
                    id=f"{pack_name}:{d.name}",
                    owner_user_id="bot",
                    owner_login="bot",
                    name=d.name,
                    body=d.body,
                    version=1,
                    summary=d.summary,
                    visibility="shareable",
                    status="active",
                    params=d.params(),
                ),
                pack,
                d.internal,
            )
            for d in derived
        ]
        return cls(base, members)

    def specs(self) -> list[CommandSpec]:
        """The members anyone can type, for listings of the built-ins: they are sentinels like `true`."""
        return [spec_for(name, command, None, pack) for name, (command, pack) in sorted(self.public.items())]

    def resolve(self, ctx: ExecContext, invocation: Invocation) -> Resolved | None:
        return self.resolve_name(ctx, invocation.name, invocation.personal)

    def resolve_name(self, ctx: ExecContext, name: str, personal: bool = False) -> Resolved | None:
        if not personal:
            member = self.public.get(name)
            pack_id = _pack_id_of(ctx)
            if member is None and pack_id is not None:
                member = self.internal.get((pack_id, name))
            if member is not None:
                return self._resolved(ctx, name, *member)
        return self.base.resolve_name(ctx, name, personal)

    def _resolved(self, ctx: ExecContext, name: str, command: CustomCommand, pack: Pack) -> Resolved | None:
        prefix = ctx.channel.prefix
        key = (command.id, command.version, prefix)
        body = self._asts.get(key)
        if body is None:
            try:
                body = self._asts[key] = CustomCommandService.parse_body(command.body, prefix)
            except Exception as exc:  # the pack script checks bodies, so this is an install gone wrong
                log.warning("customcmd.system_body_unparsable", command_id=command.id, error=repr(exc))
                return None
        target = _target(command, body, command.name, pack)
        return Resolved(spec_for(name, command, None, pack), custom=target, source="system")


def system_specs(resolver: Resolver) -> list[CommandSpec]:
    """The system packs' commands, when the runtime resolves them (ADR-0019)."""
    return resolver.specs() if isinstance(resolver, SystemResolver) else []


@dataclass(frozen=True, slots=True)
class _Key:
    name: str
    personal: bool
    #: Set for a name inside the body of a pack's command: that pack's internal member of the name.
    pack_id: str | None = None


class PreloadedResolver:
    """Built-in first, then whatever the loader found for this expression (spec §5.1). Inside a pack
    command's body, the pack's internal members come next, so a channel can't shadow them."""

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
            pack_id = _pack_id_of(ctx)
            if pack_id is not None:
                found = self.entries.get(_Key(name, False, pack_id))
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
        pending = self._names(node, None)
        seen: set[_Key] = set()
        for _ in range(MAX_CC_DEPTH + 1):
            wanted = [key for key in pending if key not in seen]
            if not wanted:
                break
            pending = []
            for key in wanted:
                seen.add(key)
                if not key.personal and base.resolve_name(ctx, key.name) is not None:
                    continue  # a built-in or system pack member of that name wins (spec §5.1)
                resolved = await self._lookup(ctx, key)
                if resolved is None:
                    continue
                entries[key] = resolved
                assert resolved.custom is not None
                pack_id = None if resolved.custom.system else resolved.custom.pack_id
                pending.extend(self._names(resolved.custom.body, pack_id))
        return PreloadedResolver(base, entries) if entries else base

    @staticmethod
    def _names(node: Node, pack_id: str | None) -> list[_Key]:
        keys: list[_Key] = []
        for inv in invocations(node):
            if pack_id is not None and not inv.personal:
                keys.append(_Key(inv.name, False, pack_id))  # the pack's own helper, if it has one
            keys.append(_Key(inv.name, inv.personal))
        return keys

    async def _lookup(self, ctx: ExecContext, key: _Key) -> Resolved | None:
        publication: Publication | None = None
        pack: Pack | None = None
        source: Source = "personal"
        command: CustomCommand | None = None
        if key.pack_id is not None:
            if self.packs is None:
                return None
            command = await self.packs.internal_member(key.pack_id, key.name)
            if command is None:
                return None
            pack, source = await self.packs.by_id(key.pack_id), "publication"
            if pack is None:
                return None
        elif key.personal:  # `@name` addresses the invoker's own alias, skipping every publication
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
        name = publication.name if publication else (command.name if pack else None)
        target = _target(command, body, name, pack)
        return Resolved(spec_for(key.name, command, publication, pack), custom=target, source=source)

    async def _personal(self, ctx: ExecContext, alias: str) -> CustomCommand | None:
        if ctx.invoker is None:
            return None
        return await self.service.personal(ctx.invoker.id, alias)
