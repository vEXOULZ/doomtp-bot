"""`customcmds` module: `!cc` — create, share, link and publish custom commands (ADR-0009).

Edits are live everywhere, so linking and publishing someone else's command answers with a warning
saying exactly that. Publishing and granting are channel-moderator actions by default; the thresholds
are the channel's `create_min_role`, `publish_min_role` and `grant_min_role`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from doomtp_bot.customcmds import params
from doomtp_bot.customcmds.packs import PackService
from doomtp_bot.customcmds.resolution import spec_for
from doomtp_bot.customcmds.service import (
    CustomCommand,
    CustomCommandService,
    Publication,
)
from doomtp_bot.lang.ast import stores
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import Context, parse
from doomtp_bot.modules._common import need, policy_of, rank, reject_filtered, user_arg
from doomtp_bot.policy.roles import BOT_ADMIN_RANK, GLOBAL
from doomtp_bot.runtime.context import Args, CommandContext, Publisher
from doomtp_bot.runtime.executor import ScopeArgs, UsageError
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Code, CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, LogLevel, Param

if TYPE_CHECKING:
    from doomtp_bot.runtime.engine import Runtime
    from doomtp_bot.variables.access import VariableAccessPolicy

MODULE = "customcmds"
USAGE = (
    "cc add <name> <expression> | edit <name> <expression> | rm <name> | list | info <name> |"
    " versions <name> | revert <name> <version> | share <name> on|off |"
    " param <name> <pos> name=<n> [type=…] [required=yes] <description> | describe <name> <summary> |"
    " run <id> [args…] |"
    " pack create|add|rm|share|list|info|delete <pack> [commands…] | publish pack <pack> [global] |"
    " link <@owner name|name> [alias] | unlink <alias> | publish <name> [as <name>] | unpublish <name> |"
    " disable|enable <name> | grant <name> <variable> | revoke <name> <variable>"
)
EDIT_WARNING = "⚠ {owner} can edit or delete it at any time, and changes apply immediately."
# Namespaces a published command can't write on its own: a channel mod grants each variable
# by name (ADR-0010, variable-access-matrix §4).
GRANTABLE = ("channel", "channel.chatter")


def _service(ctx: CommandContext) -> CustomCommandService:
    return ctx.service("customcmds")  # type: ignore[no-any-return]


def _access(ctx: CommandContext) -> VariableAccessPolicy:
    return ctx.service("variable_access")  # type: ignore[no-any-return]


def _packs(ctx: CommandContext) -> PackService:
    return ctx.service("packs")  # type: ignore[no-any-return]


def _scope(ctx: CommandContext, words: list[str]) -> str:
    """`global` at the end of a publish makes it a derived command, for bot admins only (ADR-0012)."""
    if words and words[-1].lower() == "global":
        if rank(ctx) < BOT_ADMIN_RANK:
            raise CommandError("only bot admins can publish globally", Code.DENIED)
        return GLOBAL
    return ctx.channel.id


def _where(scope: str) -> str:
    return "everywhere" if scope == GLOBAL else "here"


def _invoker(ctx: CommandContext) -> tuple[str, str]:
    if ctx.invoker is None:
        raise CommandError("cc needs a chatter")
    return ctx.invoker.id, ctx.invoker.login


def _may(ctx: CommandContext, setting: str) -> bool:
    """Is the caller at or above the channel's threshold for this action?"""
    return rank(ctx) >= BOT_ADMIN_RANK or policy_of(ctx).reaches_setting_role(ctx.exec, setting)


async def _own(ctx: CommandContext, name: str) -> CustomCommand:
    user_id, _ = _invoker(ctx)
    found = await _service(ctx).by_owner(user_id, name)
    if found is None:
        raise CommandError(f"you don't have a command named {name}")
    return found


async def _publication_here(ctx: CommandContext, name: str) -> tuple[Publication, CustomCommand]:
    found = await _service(ctx).publication(ctx.channel.id, name)
    if found is None:
        raise CommandError(f"{name} isn't published here")
    return found


async def _reachable_here(ctx: CommandContext, name: str) -> CustomCommand:
    """Whatever this channel gets under that name, in the order the runtime resolves it: this channel's
    publication, a global one, then a pack published here or globally (spec 5.1).

    Grants are per command, so a derived command that arrives globally is granted in this channel like
    any other (ADR-0012).
    """
    found = await _service(ctx).publication_in_scope(ctx.channel.id, name)
    if found is not None:
        return found[1]
    in_pack = await _packs(ctx).find_in_scope(ctx.channel.id, name)
    if in_pack is None:
        raise CommandError(f"{name} isn't published here")
    return in_pack[0]


# ── the subcommands ─────────────────────────────────────────────────────────
async def _add(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 2, USAGE)
    if not _may(ctx, "create_min_role"):
        raise CommandError("you can't create commands here", Code.DENIED)
    body = args.raw_tail or " ".join(v[2:])
    if not body:
        raise CommandError(f"usage: {USAGE}")
    user_id, login = _invoker(ctx)
    created = await _service(ctx).create(
        owner_user_id=user_id,
        owner_login=login,
        name=v[1],
        body=body,
        channel_id=ctx.channel.id,
        prefix=ctx.channel.prefix,
    )
    return Result.success(
        f"created {ctx.channel.prefix}{created.name} ({created.id}). "
        f"Use {ctx.channel.prefix}cc publish {created.name} to offer it to this channel.",
        {"id": created.id, "name": created.name},
    )


async def _edit(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 2, USAGE)
    command = await _own(ctx, v[1])
    body = args.raw_tail or " ".join(v[2:])
    if not body:
        raise CommandError(f"usage: {USAGE}")
    updated = await _service(ctx).edit(command, body, channel_id=ctx.channel.id, prefix=ctx.channel.prefix)
    links, publications = await _service(ctx).usage_of(command.id)
    where = (
        f"{links} alias{'es' if links != 1 else ''}, {publications} channel{'s' if publications != 1 else ''}"
    )
    return Result.success(f"{command.name} is now v{updated.version}, live in {where}", updated.version)


async def _rm(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 2, USAGE)
    command = await _own(ctx, v[1])
    links, publications = await _service(ctx).delete(command)
    return Result.success(
        f"deleted {command.name}; {links} alias(es) and {publications} channel(s) stopped working"
    )


async def _list(ctx: CommandContext, v: list[str], args: Args) -> Result:
    user_id, _ = _invoker(ctx)
    service = _service(ctx)
    owned = await service.owned_by(user_id)
    linked = [(alias, c) for alias, c in await service.linked_by(user_id) if c.owner_user_id != user_id]
    parts = [f"yours: {', '.join(c.name for c in owned) or 'none'}"]
    if linked:
        parts.append("linked: " + ", ".join(f"{alias} (by @{c.owner_login})" for alias, c in linked))
    return Result.success(
        "; ".join(parts),
        {"owned": [c.name for c in owned], "linked": {alias: c.id for alias, c in linked}},
    )


async def _info(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 2, USAGE)
    service = _service(ctx)
    name = v[1]
    found = await service.publication(ctx.channel.id, name)
    publication, command = found if found else (None, None)
    if command is None:
        user_id, _ = _invoker(ctx)
        command = await service.personal(user_id, name) or await service.by_owner(user_id, name)
    if command is None:
        return Result.failure(Code.NOT_FOUND, f"no command named {name} here")
    links, publications = await service.usage_of(command.id)
    spec = spec_for(command.name, command, publication)
    text = (
        f"{ctx.channel.prefix}{spec.usage()} ({command.id}) by @{command.owner_login}, v{command.version}, "
        f"{links} alias(es), {publications} channel(s). "
        f"{params.describe(params.to_params(command.params))}: {command.body}"
    )
    if publication is not None and publication.last_run_version not in (None, command.version):
        text += f" — changed since v{publication.last_run_version} by @{command.owner_login}"
    return Result.success(
        text,
        {
            "id": command.id,
            "owner": command.owner_login,
            "version": command.version,
            "body": command.body,
            "published_as": publication.name if publication else None,
        },
    )


async def _versions(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 2, USAGE)
    command = await _own(ctx, v[1])
    history = await _service(ctx).versions(command.id)
    listing = ", ".join(f"v{version}: {body}" for version, body, _ in history[:5])
    return Result.success(f"{command.name}: {listing}", [v for v, _, _ in history])


async def _revert(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 3, USAGE)
    command = await _own(ctx, v[1])
    if not v[2].isdigit():
        raise CommandError("version must be a number")
    updated = await _service(ctx).revert(command, int(v[2]))
    return Result.success(f"{command.name} reverted to v{v[2]}, now v{updated.version}: {updated.body}")


async def _param(ctx: CommandContext, v: list[str], args: Args) -> Result:
    """`cc param <name> <pos> name=<n> [type=…] [required=yes] "<description>"`, or `<pos> remove`."""
    need(v, 3, USAGE)
    command = await _own(ctx, v[1])
    position = v[2]
    if len(v) > 3 and v[3].lower() == "remove":
        rows = params.remove(command.params, position)
    else:
        reject_filtered(ctx, " ".join(v[3:]))
        assignments, description = params.split_declaration(" ".join(v[3:]))
        try:
            rows = params.declare(command.params, position, assignments, description)
        except params.ParamError as exc:
            raise CommandError(str(exc)) from exc
    updated = await _service(ctx).set_params(command, rows)
    declared = params.to_params(updated.params)
    spec = spec_for(command.name, updated, None)
    return Result.success(
        f"{ctx.channel.prefix}{spec.usage()} — {params.describe(declared)}",
        [dict(r) for r in rows],
    )


async def _describe(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 3, USAGE)
    command = await _own(ctx, v[1])
    summary = " ".join(v[2:])
    reject_filtered(ctx, summary)
    await _service(ctx).set_summary(command, summary)
    return Result.success(f"{command.name}: {summary}")


async def _pack(ctx: CommandContext, v: list[str], args: Args) -> Result:
    """`cc pack create|add|rm|list|info|delete <pack> [commands…]`."""
    need(v, 2, USAGE)
    action, packs, (user_id, _) = v[1].lower(), _packs(ctx), _invoker(ctx)
    if action == "list":
        owned = await packs.owned_by(user_id)
        sizes = [(p, len(await packs.members(p.id))) for p in owned]
        listing = ", ".join(f"{p.name} ({n})" for p, n in sizes) or "none"
        return Result.success(f"your packs: {listing}", [p.name for p in owned])
    need(v, 3, USAGE)
    name = v[2].lower()
    if action == "create":
        reject_filtered(ctx, name, " ".join(v[3:]))
        created = await packs.create(owner_user_id=user_id, name=name, summary=" ".join(v[3:]))
        return Result.success(
            f"created pack {created.name}. Add commands with "
            f"{ctx.channel.prefix}cc pack add {created.name} <command…>"
        )
    pack = await packs.by_owner(user_id, name)
    if pack is None:
        raise CommandError(f"you have no pack named {name}")
    if action == "info":
        members = await packs.members(pack.id)
        published = [
            ("everywhere" if p.is_global else "here")
            for p, k in await packs.publications_in(ctx.channel.id, include_global=True)
            if k.id == pack.id and p.status == "active"
        ]
        where = ", ".join(published) or "not published here"
        return Result.success(
            f"{pack.name}: {', '.join(c.name for c in members) or 'empty'} — {where}",
            {"pack": pack.name, "commands": [c.name for c in members], "published": published},
        )
    if action == "delete":
        await packs.delete(pack)
        return Result.success(f"deleted pack {pack.name}; it is no longer published anywhere")
    if action == "share":
        need(v, 4, USAGE)
        if v[3].lower() not in ("on", "off"):
            raise CommandError(f"usage: {ctx.channel.prefix}cc pack share <pack> on|off")
        shareable = v[3].lower() == "on"
        members = await packs.members(pack.id)
        for member in members:
            await _service(ctx).set_visibility(member, shareable)
        state = "shareable" if shareable else "private"
        return Result.success(
            f"{pack.name} and its {len(members)} command(s) are {state}"
            + (
                f"; mods can {ctx.channel.prefix}cc publish pack @{ctx.invoker.login if ctx.invoker else ''} {pack.name}"
                if shareable
                else ""
            )
        )
    if action not in ("add", "rm"):
        raise CommandError(f"usage: {USAGE}")
    need(v, 4, USAGE)
    changed: list[str] = []
    for command_name in v[3:]:
        command = await _service(ctx).by_owner(user_id, command_name)
        if command is None:
            raise CommandError(f"you don't have a command named {command_name}")
        if action == "add":
            await packs.add_member(pack, command)
            changed.append(command.name)
        elif await packs.remove_member(pack, command):
            changed.append(command.name)
    verb = "added to" if action == "add" else "removed from"
    return Result.success(f"{', '.join(changed) or 'nothing'} {verb} {pack.name}", changed)


async def _share(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 3, USAGE)
    command = await _own(ctx, v[1])
    if v[2].lower() not in ("on", "off"):
        raise CommandError(f"usage: {ctx.channel.prefix}cc share <name> on|off")
    shareable = v[2].lower() == "on"
    await _service(ctx).set_visibility(command, shareable)
    how = f"anyone can {ctx.channel.prefix}cc link @{command.owner_login} {command.name}"
    return Result.success(
        f"{command.name} is {'shareable' if shareable else 'private'}" + (f"; {how}" if shareable else "")
    )


async def _link(ctx: CommandContext, v: list[str], args: Args) -> Result:
    """`cc link <name>` links what this channel publishes; `cc link @owner <name>` links theirs."""
    need(v, 2, USAGE)
    service, user_id = _service(ctx), _invoker(ctx)[0]
    if v[1].startswith("@"):
        need(v, 3, USAGE)
        owner = await user_arg(ctx, v[1])
        command = await service.by_owner(owner["id"], v[2])
        if command is None or not command.shareable:
            raise CommandError(f"@{owner['name']} has no shared command named {v[2]}")
        alias = v[3] if len(v) > 3 else command.name
    else:
        _, command = await _publication_here(ctx, v[1])
        alias = v[2] if len(v) > 2 else v[1]
    if command.owner_user_id == user_id:
        raise CommandError("that's your own command")
    reject_filtered(ctx, alias)
    await service.link(user_id=user_id, alias=alias, command=command)
    warning = EDIT_WARNING.format(owner=f"@{command.owner_login}")
    return Result.success(
        f'linked "{command.name}" (by @{command.owner_login}) as {ctx.channel.prefix}{alias}. {warning}',
        {"alias": alias, "id": command.id},
    )


async def _unlink(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 2, USAGE)
    user_id, _ = _invoker(ctx)
    removed = await _service(ctx).unlink(user_id=user_id, alias=v[1])
    return Result.success(f"unlinked {v[1]}" if removed else f"you have no alias named {v[1]}")


def _channel_writes(ctx: CommandContext, custom: CustomCommand) -> list[str]:
    """The channel variables a body writes. Published, it can't write them without a grant (ADR-0010)."""
    runtime: Runtime = ctx.service("runtime")
    try:
        node = parse(custom.body, Context.BODY, runtime.parser_params(ctx.channel.prefix))
    except ParseError:
        return []  # a body that no longer parses is somebody else's problem to report
    seen = {f"{s.target.namespace}.{s.target.name}" for s in stores(node) if s.target.namespace in GRANTABLE}
    return sorted(seen)


def _grant_warning(ctx: CommandContext, commands: list[CustomCommand], scope: str) -> str:
    """Say up front which writes stay denied until a mod grants them, and how to grant them."""
    wanted: dict[str, list[str]] = {}
    for member in commands:
        writes = _channel_writes(ctx, member)
        if not writes:
            continue
        held = frozenset() if scope == GLOBAL else _access(ctx).granted(scope, member.id)
        missing = [variable for variable in writes if variable not in held]
        if missing:
            wanted[member.name] = missing
    if not wanted:
        return ""
    listed = "; ".join(f"{name} writes {', '.join(writes)}" for name, writes in sorted(wanted.items()))
    where = " in each channel that enables it" if scope == GLOBAL else ""
    first = next(iter(sorted(wanted)))
    return (
        f" ⚠ {listed} — those writes are denied{where} until a mod allows them:"
        f" {ctx.channel.prefix}cc grant {first} {wanted[first][0]}."
    )


async def _publish(ctx: CommandContext, v: list[str], args: Args) -> Result:
    """`cc publish <own name|alias> [as <name>] [global]`, or `cc publish pack <name> [global]`."""
    need(v, 2, USAGE)
    scope = _scope(ctx, v)
    if scope != GLOBAL and not _may(ctx, "publish_min_role"):
        raise CommandError("you can't publish commands here", Code.DENIED)
    if v[1].lower() == "pack":
        return await _publish_pack(ctx, v, scope)
    service, (user_id, _) = _service(ctx), _invoker(ctx)
    command = await service.by_owner(user_id, v[1]) or await service.personal(user_id, v[1])
    if command is None:
        raise CommandError(f"you have no command or alias named {v[1]}")
    name = v[3] if len(v) > 3 and v[2].lower() == "as" else command.name
    reject_filtered(ctx, name)
    await service.publish(channel_id=scope, name=name, command=command, published_by=user_id)
    text = (
        f'published "{command.name}" (by @{command.owner_login}) as '
        f"{ctx.channel.prefix}{name} {_where(scope)}."
    )
    if command.owner_user_id != user_id:
        text += " " + EDIT_WARNING.format(owner=f"@{command.owner_login}")
    text += f" Mods can {ctx.channel.prefix}cc disable {name}."
    return Result.success(text + _grant_warning(ctx, [command], scope), {"name": name})


async def _resolve_pack(ctx: CommandContext, v: list[str], at: int) -> Any:
    """`<name>` is the caller's own pack; `@owner <name>` is someone else's, if they shared its commands."""
    user_id, _ = _invoker(ctx)
    packs = _packs(ctx)
    if v[at].startswith("@"):
        need(v, at + 2, USAGE)
        owner = await user_arg(ctx, v[at])
        pack = await packs.by_owner(owner["id"], v[at + 1])
        if pack is None:
            raise CommandError(f"@{owner['name']} has no pack named {v[at + 1]}")
        members = await packs.members(pack.id)
        if not members or not all(c.shareable for c in members):
            raise CommandError(f"@{owner['name']} hasn't shared every command in {pack.name}")
        return pack
    pack = await packs.by_owner(user_id, v[at])
    if pack is None:
        raise CommandError(f"you have no pack named {v[at]}")
    return pack


async def _publish_pack(ctx: CommandContext, v: list[str], scope: str) -> Result:
    need(v, 3, USAGE)
    user_id, _ = _invoker(ctx)
    packs = _packs(ctx)
    pack = await _resolve_pack(ctx, v, 2)
    members = await packs.members(pack.id)
    if not members:
        raise CommandError(f"{pack.name} has no commands yet")
    await packs.publish(channel_id=scope, pack=pack, published_by=user_id)
    names = ", ".join(c.name for c in members)
    owner_note = ""
    if pack.owner_user_id != user_id:
        owners = {c.owner_login for c in members}
        owner_note = " " + EDIT_WARNING.format(owner="@" + ", @".join(sorted(owners)))
    return Result.success(
        f"published pack {pack.name} {_where(scope)} ({names}).{owner_note} "
        f"⚠ Commands added to the pack later appear {_where(scope)} too. "
        f"Mods can {ctx.channel.prefix}module disable {pack.name}."
        + _grant_warning(ctx, list(members), scope),
        {"pack": pack.name, "commands": [c.name for c in members]},
    )


async def _unpublish(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 2, USAGE)
    scope = _scope(ctx, v)
    if scope != GLOBAL and not _may(ctx, "publish_min_role"):
        raise CommandError("you can't unpublish commands here", Code.DENIED)
    user_id, _ = _invoker(ctx)
    if v[1].lower() == "pack":
        need(v, 3, USAGE)
        pack = await _resolve_pack(ctx, v, 2)
        if not await _packs(ctx).unpublish(channel_id=scope, pack=pack, actor_user_id=user_id):
            raise CommandError(f"{pack.name} isn't published {_where(scope)}")
        return Result.success(f"unpublished pack {pack.name} {_where(scope)}; its write grants went too")
    if await _service(ctx).unpublish(channel_id=scope, name=v[1], actor_user_id=user_id) is None:
        raise CommandError(f"{v[1]} isn't published {_where(scope)}")
    return Result.success(f"unpublished {v[1]} {_where(scope)}; its write grants there were revoked too")


async def _set_status(ctx: CommandContext, v: list[str], enabled: bool) -> Result:
    need(v, 2, USAGE)
    if not _may(ctx, "publish_min_role"):
        raise CommandError("only channel moderators can do that", Code.DENIED)
    user_id, _ = _invoker(ctx)
    changed = await _service(ctx).set_publication_status(
        channel_id=ctx.channel.id,
        name=v[1],
        status="active" if enabled else "disabled",
        actor_user_id=user_id,
    )
    if not changed:
        raise CommandError(f"{v[1]} isn't published here")
    return Result.success(f"{v[1]} {'enabled' if enabled else 'disabled'} here")


async def _grant(ctx: CommandContext, v: list[str], granted: bool) -> Result:
    """Let a published command write one exact channel variable (variable-access-matrix.md §4)."""
    need(v, 3, USAGE)
    if not _may(ctx, "grant_min_role"):
        raise CommandError("only channel moderators can grant variable writes", Code.DENIED)
    command = await _reachable_here(ctx, v[1])
    user_id, _ = _invoker(ctx)
    try:
        await _access(ctx).set_grant(ctx.channel.id, command.id, v[2].lower(), granted, user_id)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    if not granted:
        return Result.success(f"{v[1]} can no longer write {v[2]}")
    return Result.success(
        f"{v[1]} can now write {v[2]}. ⚠ That stays true after future edits by @{command.owner_login}."
    )


async def _run(ctx: CommandContext, v: list[str], args: Args) -> Result:
    """`cc run <id> [args…]` runs a command by its id, skipping name resolution (ADR-0009 §5).

    The owner's escape hatch: try a command that is published nowhere, or one whose name a built-in
    has taken. A *shared* command runs for anybody this way — the same door `cc link @owner <name>`
    opens — and a private one stays private.

    Typed in chat only. A body that could reach `cc run` would recurse past the depth and cycle
    checks preflight does for names, and `{sign}cc run` inside a body is not a thing anyone needs.
    """
    need(v, 2, USAGE)
    if ctx.exec.context is not Context.LINE:
        raise CommandError("cc run only works typed in chat")
    user_id, _ = _invoker(ctx)
    command = await _service(ctx).by_id(v[1])
    if command is None:
        raise CommandError(f"no command with id {v[1]}")
    if command.owner_user_id != user_id and not command.shareable:
        raise CommandError(f"{command.id} isn't shared", Code.DENIED)

    runtime: Runtime = ctx.service("runtime")
    values = tuple(v[2:])
    body_ctx = runtime.make_context(
        channel=ctx.channel,
        invoker=ctx.invoker,
        context=Context.BODY,
        trigger_type=ctx.exec.trigger_type,
        message_id=ctx.exec.message_id,
        run_as_rank=ctx.exec.run_as_rank,
        dry_run=ctx.exec.dry_run,
        is_cancelled=ctx.exec.is_cancelled,
        rng=ctx.exec.rng,
        clock=ctx.exec.clock,
        bot=ctx.exec.bot,
    )
    spec = spec_for(command.name, command, None)
    try:
        params = await runtime.executor.bind(spec, values, body_ctx)
    except UsageError as exc:
        raise CommandError(f"usage: {ctx.channel.prefix}{spec.usage()} — {exc}") from exc
    report = await runtime.run(
        command.body,
        body_ctx,
        scope_args=ScopeArgs.of(values, params),
        publisher=Publisher(
            id=command.owner_user_id,
            login=command.owner_login,
            command_id=command.id,
            command_name=command.name,
            alias=command.name,
            version=command.version,
        ),
    )
    if report is None:  # Body context parses everything, or fails loudly
        raise CommandError(f"{command.id} has an empty body")
    if report.result.code >= 100:  # timed out, denied, cancelled: the runtime owns those codes
        raise CommandError(report.result.message or f"{command.id} didn't finish", report.result.code)
    return report.result


_SUBCOMMANDS = {
    "add": _add,
    "edit": _edit,
    "rm": _rm,
    "delete": _rm,
    "list": _list,
    "info": _info,
    "versions": _versions,
    "revert": _revert,
    "share": _share,
    "param": _param,
    "pack": _pack,
    "describe": _describe,
    "run": _run,
    "link": _link,
    "unlink": _unlink,
    "publish": _publish,
    "unpublish": _unpublish,
}


@command(
    CommandSpec(
        name="cc",
        module=MODULE,
        summary="Create and share custom commands",
        description=USAGE,
        params=(Param("1+", "arguments", description=USAGE),),
        examples=(
            Example("{sign}cc add hype echo {chatter.display} is hyped!", "created {sign}hype (cc_7f3k2)"),
            Example("{sign}cc publish hype", 'published "hype" as {sign}hype'),
            Example(
                "{sign}cc run cc_7f3k2 world",
                "hello world",
                note="runs a command by id, whatever it is published as",
            ),
        ),
        default_cooldowns={"everyone": Cooldown(tier_s=0, user_s=5)},
        log_level=LogLevel.INVOCATIONS,
    ),
    raw_tail_subcommands=(("add", 3), ("edit", 3)),
)
async def cc_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    values = list(args.values)
    need(values, 1, USAGE)
    action = values[0].lower()
    handler: Any = _SUBCOMMANDS.get(action)
    if handler is not None:
        return await handler(ctx, values, args)  # type: ignore[no-any-return]
    if action in ("disable", "enable"):
        return await _set_status(ctx, values, action == "enable")
    if action in ("grant", "revoke"):
        return await _grant(ctx, values, action == "grant")
    raise CommandError(f"usage: {USAGE}")


COMMANDS: tuple[Command, ...] = (cc_cmd,)
