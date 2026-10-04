"""Custom commands, packs and publications on the web: what `!cc` does in chat (ADR-0009, ADR-0026).

`/me/…` is the signed-in user's own: their commands, aliases, packs, variables and runs, open to anyone
signed in with Twitch, whether or not they manage a channel. `/channels/{login}/…` is what a channel
offers: publishing, packs and write grants, for whoever reaches the channel's `publish_min_role` or
`grant_min_role`, as in chat. Every route goes through the services `!cc` uses, so the rules and the
audit entries are the same; only `via` differs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from doomtp_bot.api.access import PERSONAL_READ, PERSONAL_WRITE, Caller, check_setting_role
from doomtp_bot.api.routes.data import MAX_ROWS, _channel, _custom_json, _policy, _state
from doomtp_bot.api.routes.manage import _filter_check
from doomtp_bot.customcmds import params
from doomtp_bot.customcmds.packs import Pack, PackService, SystemPackError
from doomtp_bot.customcmds.service import CustomCommand, CustomCommandError, CustomCommandService
from doomtp_bot.lang.ast import stores
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import DEFAULT_PREFIX, Context, parse
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.policy.snapshot import ChannelSettings
from doomtp_bot.variables.store import Entry

router = APIRouter(prefix="/api/v1", tags=["commands"])

# Namespaces a published command can't write on its own; a channel mod grants each variable (ADR-0010).
GRANTABLE = ("channel", "channel.chatter")


def _commands(request: Request) -> CustomCommandService:
    return _state(request, "customcmds")  # type: ignore[no-any-return]


def _packs(request: Request) -> PackService:
    return _state(request, "packs")  # type: ignore[no-any-return]


def _me(caller: Caller) -> tuple[str, str]:
    """The signed-in Twitch user. A key or the password has no commands of its own."""
    if caller.user_id is None:
        raise HTTPException(status_code=403, detail="sign in with Twitch to have your own commands")
    return caller.user_id, caller.login or caller.user_id


def _refused(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _where_from(request: Request, login: str | None) -> tuple[str, str]:
    """The channel a body is written from: its filter and command sign apply. None: the bot-wide ones."""
    if login is None:
        return GLOBAL, DEFAULT_PREFIX
    settings = _channel(request, login)
    return settings.channel_id, settings.prefix


async def _own(request: Request, user_id: str, name: str) -> CustomCommand:
    found = await _commands(request).by_owner(user_id, name)
    if found is None:
        raise HTTPException(status_code=404, detail=f"you don't have a command named {name}")
    return found


def _logins(request: Request) -> dict[str, str]:
    return {c.channel_id: c.login for c in _policy(request).channels()}


# ── my commands ─────────────────────────────────────────────────────────────
@router.get("/me/custom-commands")
async def my_commands(request: Request, caller: Caller = PERSONAL_READ) -> dict[str, Any]:
    """`cc list`, with where each command is published and how many use it."""
    user_id, _ = _me(caller)
    service, logins = _commands(request), _logins(request)
    owned: list[dict[str, Any]] = []
    for command in await service.owned_by(user_id):
        links, _count = await service.usage_of(command.id)
        owned.append(
            {
                **_custom_json(command),
                "links": links,
                "publications": [
                    {
                        "channel": "global" if p.channel_id == GLOBAL else logins.get(p.channel_id, p.channel_id),
                        "name": p.name,
                        "status": p.status,
                    }
                    for p in await service.publications_of(command.id)
                ],
            }
        )
    linked = [
        {"alias": alias, **_custom_json(c)}
        for alias, c in await service.linked_by(user_id)
        if c.owner_user_id != user_id
    ]
    return {"commands": owned, "linked": linked, "quota": service.quota}


class NewCommand(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    body: str = Field(min_length=1, max_length=2000)
    summary: str = Field(default="", max_length=200)
    # Where it is written from, as chat's `cc add` is typed in a channel: that channel's `create_min_role`,
    # filter and command sign apply.
    channel: str = Field(min_length=1, max_length=40)


@router.post("/me/custom-commands", status_code=201)
async def create_command(request: Request, body: NewCommand, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    user_id, login = _me(caller)
    settings = _channel(request, body.channel)
    check_setting_role(request, caller, settings.login, "create_min_role", "create commands")
    service = _commands(request)
    if body.summary:
        _filter_check(request, settings.channel_id, body.summary)
    try:
        created = await service.create(
            owner_user_id=user_id,
            owner_login=login,
            name=body.name,
            body=body.body,
            channel_id=settings.channel_id,
            prefix=settings.prefix,
            actor_via=caller.actor.via,
        )
    except CustomCommandError as exc:
        raise _refused(exc) from exc
    if body.summary:
        await service.set_summary(created, body.summary, actor_via=caller.actor.via)
        created = await _own(request, user_id, created.name)
    return _custom_json(created)


class CommandPatch(BaseModel):
    body: str | None = Field(default=None, min_length=1, max_length=2000)
    summary: str | None = Field(default=None, max_length=200)
    shareable: bool | None = None
    # The channel the new body is checked in (filter, command sign); the bot-wide filter when left out.
    channel: str | None = Field(default=None, max_length=40)


@router.patch("/me/custom-commands/{name}")
async def edit_command(
    request: Request, name: str, body: CommandPatch, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    """`cc edit`, `cc describe` and `cc share`, any of them in one call."""
    user_id, _ = _me(caller)
    service, via = _commands(request), caller.actor.via
    command = await _own(request, user_id, name)
    channel_id, prefix = _where_from(request, body.channel)
    if body.summary is not None:
        _filter_check(request, channel_id, body.summary)
    if body.body is not None and body.body != command.body:
        try:
            await service.edit(command, body.body, channel_id=channel_id, prefix=prefix, actor_via=via)
        except CustomCommandError as exc:
            raise _refused(exc) from exc
    if body.summary is not None and body.summary != command.summary:
        await service.set_summary(command, body.summary, actor_via=via)
    if body.shareable is not None and body.shareable != command.shareable:
        await service.set_visibility(command, body.shareable, actor_via=via)
    return _custom_json(await _own(request, user_id, name))


@router.delete("/me/custom-commands/{name}")
async def delete_command(request: Request, name: str, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    user_id, _ = _me(caller)
    command = await _own(request, user_id, name)
    links, publications = await _commands(request).delete(command, actor_via=caller.actor.via)
    return {"name": command.name, "removed": True, "links": links, "publications": publications}


@router.get("/me/custom-commands/{name}/versions")
async def command_versions(request: Request, name: str, caller: Caller = PERSONAL_READ) -> dict[str, Any]:
    user_id, _ = _me(caller)
    command = await _own(request, user_id, name)
    history = await _commands(request).versions(command.id)
    return {
        "name": command.name,
        "current": command.version,
        "versions": [{"version": v, "body": b, "created_at": at} for v, b, at in history],
    }


class RevertBody(BaseModel):
    version: int = Field(ge=1)


@router.post("/me/custom-commands/{name}/revert")
async def revert_command(
    request: Request, name: str, body: RevertBody, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    """`cc revert`: the old body comes back as a new version."""
    user_id, _ = _me(caller)
    command = await _own(request, user_id, name)
    try:
        updated = await _commands(request).revert(command, body.version, actor_via=caller.actor.via)
    except CustomCommandError as exc:
        raise _refused(exc) from exc
    return _custom_json(updated)


class ParamBody(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    type: str | None = Field(default=None, max_length=20)
    required: bool | None = None
    default: str | None = Field(default=None, max_length=200)
    min: float | None = None
    max: float | None = None
    max_len: int | None = Field(default=None, ge=1)
    choices: list[str] | None = Field(default=None, max_length=50)
    description: str = Field(default="", max_length=200)


def _assignments(body: ParamBody) -> list[str]:
    """The `key=value` words `cc param` takes, so `params.declare` checks the web's declaration too."""
    words = [f"name={body.name}"]
    if body.type is not None:
        words.append(f"type={body.type}")
    if body.required is not None:
        words.append(f"required={'yes' if body.required else 'no'}")
    if body.default is not None:
        words.append(f"default={body.default}")
    for key in ("min", "max"):
        value = getattr(body, key)
        if value is not None:
            words.append(f"{key}={int(value) if float(value).is_integer() else value}")
    if body.max_len is not None:
        words.append(f"max_len={body.max_len}")
    if body.choices is not None:
        words.append("choices=" + ",".join(body.choices))
    return words


@router.put("/me/custom-commands/{name}/params/{position}")
async def declare_param(
    request: Request, name: str, position: str, body: ParamBody, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    """`cc param <name> <pos> name=… type=… "<description>"`."""
    user_id, _ = _me(caller)
    command = await _own(request, user_id, name)
    _filter_check(request, GLOBAL, body.name, body.description, *(body.choices or ()))
    try:
        rows = params.declare(command.params, position, _assignments(body), body.description)
    except params.ParamError as exc:
        raise _refused(exc) from exc
    updated = await _commands(request).set_params(command, rows, actor_via=caller.actor.via)
    return _custom_json(updated)


@router.delete("/me/custom-commands/{name}/params/{position}")
async def remove_param(request: Request, name: str, position: str, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    user_id, _ = _me(caller)
    command = await _own(request, user_id, name)
    rows = params.remove(command.params, position)
    if len(rows) == len(command.params):
        raise HTTPException(status_code=404, detail=f"{command.name} has no parameter at {position}")
    updated = await _commands(request).set_params(command, rows, actor_via=caller.actor.via)
    return _custom_json(updated)


# ── my aliases (`cc link`) ──────────────────────────────────────────────────
class LinkBody(BaseModel):
    """Someone's shared command (`owner` + `command`), or what a channel publishes (`channel` + `command`)."""

    command: str = Field(min_length=1, max_length=32)
    owner: str | None = Field(default=None, max_length=40)
    channel: str | None = Field(default=None, max_length=40)


@router.put("/me/links/{alias}")
async def link_command(request: Request, alias: str, body: LinkBody, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    user_id, _ = _me(caller)
    service = _commands(request)
    if (body.owner is None) == (body.channel is None):
        raise HTTPException(status_code=400, detail="name either an owner or a channel")
    if body.owner is not None:
        owner = await _state(request, "twitch").resolve_user(body.owner.lstrip("@"))
        command = await service.by_owner(owner["id"], body.command) if owner else None
        if command is None or not command.shareable:
            raise HTTPException(status_code=404, detail=f"@{body.owner} has no shared command named {body.command}")
    else:
        settings = _channel(request, body.channel or "")
        found = await service.publication(settings.channel_id, body.command)
        if found is None:
            raise HTTPException(status_code=404, detail=f"{body.command} isn't published in {settings.login}")
        command = found[1]
    if command.owner_user_id == user_id:
        raise HTTPException(status_code=400, detail="that's your own command")
    _filter_check(request, GLOBAL, alias)
    try:
        await service.link(user_id=user_id, alias=alias, command=command, actor_via=caller.actor.via)
    except CustomCommandError as exc:
        raise _refused(exc) from exc
    return {"alias": alias.lower(), **_custom_json(command)}


@router.delete("/me/links/{alias}")
async def unlink_command(request: Request, alias: str, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    user_id, _ = _me(caller)
    if not await _commands(request).unlink(user_id=user_id, alias=alias, actor_via=caller.actor.via):
        raise HTTPException(status_code=404, detail=f"you have no alias named {alias}")
    return {"alias": alias.lower(), "removed": True}


# ── my packs (`cc pack …`) ──────────────────────────────────────────────────
async def _pack_json(request: Request, pack: Pack) -> dict[str, Any]:
    packs, logins = _packs(request), _logins(request)
    members = await packs.members(pack.id)
    internal = await packs.internal_names(pack.id)
    published = [
        "global" if scope == GLOBAL else logins.get(scope, scope) for scope in await _pack_scopes(request, pack)
    ]
    return {
        "id": pack.id,
        "name": pack.name,
        "summary": pack.summary,
        "system": pack.is_system,
        "commands": [{"name": c.name, "internal": c.name in internal, "shareable": c.shareable} for c in members],
        "shareable": bool(members) and all(c.shareable for c in members),
        "published": published,
    }


async def _pack_scopes(request: Request, pack: Pack) -> list[str]:
    """Where a pack is published: channel ids, or GLOBAL."""
    async with await _packs(request).conn.execute(
        "SELECT channel_id FROM custom_command_pack_publications WHERE pack_id = %s AND status = 'active'"
        " ORDER BY channel_id",
        (pack.id,),
    ) as cur:
        return [str(r["channel_id"]) for r in await cur.fetchall()]


async def _own_pack(request: Request, user_id: str, name: str, *, writable: bool = True) -> Pack:
    pack = await _packs(request).by_owner(user_id, name)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"you have no pack named {name}")
    if writable and pack.is_system:
        raise _refused(SystemPackError(pack))
    return pack


@router.get("/me/packs")
async def my_packs(request: Request, caller: Caller = PERSONAL_READ) -> dict[str, Any]:
    user_id, _ = _me(caller)
    return {"packs": [await _pack_json(request, p) for p in await _packs(request).owned_by(user_id)]}


class NewPack(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    summary: str = Field(default="", max_length=200)


@router.post("/me/packs", status_code=201)
async def create_pack(request: Request, body: NewPack, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    user_id, _ = _me(caller)
    _filter_check(request, GLOBAL, body.name, body.summary)
    try:
        pack = await _packs(request).create(
            owner_user_id=user_id, name=body.name, summary=body.summary, actor_via=caller.actor.via
        )
    except CustomCommandError as exc:
        raise _refused(exc) from exc
    return await _pack_json(request, pack)


class PackPatch(BaseModel):
    shareable: bool


@router.patch("/me/packs/{name}")
async def share_pack(request: Request, name: str, body: PackPatch, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    """`cc pack share`: every member becomes shareable (or private), so others may publish the pack."""
    user_id, _ = _me(caller)
    pack = await _own_pack(request, user_id, name)
    service = _commands(request)
    for member in await _packs(request).members(pack.id):
        if member.shareable != body.shareable:
            await service.set_visibility(member, body.shareable, actor_via=caller.actor.via)
    return await _pack_json(request, pack)


@router.delete("/me/packs/{name}")
async def delete_pack(request: Request, name: str, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    user_id, _ = _me(caller)
    pack = await _own_pack(request, user_id, name)
    await _packs(request).delete(pack, actor_via=caller.actor.via)
    return {"name": pack.name, "removed": True}


class MemberBody(BaseModel):
    internal: bool | None = None


@router.put("/me/packs/{name}/commands/{command}")
async def add_pack_member(
    request: Request, name: str, command: str, body: MemberBody, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    """`cc pack add`, and `cc pack internal … on|off` when `internal` is given."""
    user_id, _ = _me(caller)
    pack = await _own_pack(request, user_id, name)
    member = await _own(request, user_id, command)
    packs, via = _packs(request), caller.actor.via
    try:
        await packs.add_member(pack, member, actor_via=via)
    except CustomCommandError as exc:
        raise _refused(exc) from exc
    if body.internal is not None:
        await packs.set_internal(pack, member, body.internal, actor_via=via)
    return await _pack_json(request, pack)


@router.delete("/me/packs/{name}/commands/{command}")
async def remove_pack_member(
    request: Request, name: str, command: str, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    user_id, _ = _me(caller)
    pack = await _own_pack(request, user_id, name)
    member = await _own(request, user_id, command)
    if not await _packs(request).remove_member(pack, member, actor_via=caller.actor.via):
        raise HTTPException(status_code=404, detail=f"{member.name} isn't in {pack.name}")
    return await _pack_json(request, pack)


# ── my variables and runs ───────────────────────────────────────────────────
def _entry_json(entry: Entry, logins: dict[str, str]) -> dict[str, Any]:
    key = entry.key
    channel = key.key1 if key.ns == "channel.chatter" else None
    return {
        "namespace": key.ns,
        "name": key.name,
        "channel": logins.get(channel, channel) if channel else None,
        "keys": [k for k in (key.key1, key.key2, key.key3) if k],
        "value": entry.value,
        "updated_at": entry.updated_at,
        "updated_by": entry.updated_by,
    }


@router.get("/me/variables")
async def my_variables(request: Request, caller: Caller = PERSONAL_READ) -> dict[str, Any]:
    """What the user owns: `chatter.*`, their `channel.chatter.*` everywhere, and their commands' `publisher.*`."""
    user_id, _ = _me(caller)
    entries = await _state(request, "variable_store").entries_of_user(user_id)
    logins = _logins(request)
    return {"variables": [_entry_json(e, logins) for e in entries]}


@router.get("/me/runs")
async def my_runs(
    request: Request, limit: int = Query(default=50, ge=1, le=MAX_ROWS), caller: Caller = PERSONAL_READ
) -> dict[str, Any]:
    """The user's own runs, in every channel."""
    user_id, _ = _me(caller)
    conn, logins = _state(request, "chatlog_db"), _logins(request)
    async with await conn.execute(
        "SELECT channel_id, trigger_type, expr, code, message, duration_ms, cancelled_reason, at"
        " FROM command_runs WHERE user_id = %s ORDER BY at DESC LIMIT %s",
        (user_id, limit),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    return {"runs": [{**r, "channel": logins.get(r["channel_id"], r["channel_id"])} for r in rows]}


# ── publishing (`cc publish`, `cc unpublish`, `cc publish pack`) ────────────
class PublishBody(BaseModel):
    command: str = Field(min_length=1, max_length=32)  # one of the caller's commands, or one of their aliases
    name: str | None = Field(default=None, max_length=32)  # `as <name>`


async def publish_in(request: Request, scope: str, body: PublishBody, caller: Caller, check_in: str) -> dict[str, Any]:
    """Publish the caller's command or alias to a channel, or everywhere (`scope` GLOBAL)."""
    user_id, _ = _me(caller)
    service = _commands(request)
    command = await service.by_owner(user_id, body.command) or await service.personal(user_id, body.command)
    if command is None:
        raise HTTPException(status_code=404, detail=f"you have no command or alias named {body.command}")
    name = body.name or command.name
    _filter_check(request, check_in, name)
    try:
        await service.publish(
            channel_id=scope, name=name, command=command, published_by=user_id, actor_via=caller.actor.via
        )
    except CustomCommandError as exc:
        raise _refused(exc) from exc
    return {
        "name": name.lower(),
        **_custom_json(command),
        "needs_grants": _missing_grants(request, scope, [command]),
    }


async def unpublish_in(request: Request, scope: str, name: str, caller: Caller) -> dict[str, Any]:
    removed = await _commands(request).unpublish(
        channel_id=scope, name=name, actor_user_id=caller.actor.user_id, actor_via=caller.actor.via
    )
    if removed is None:
        raise HTTPException(status_code=404, detail=f"{name} isn't published there")
    return {"name": name.lower(), "removed": True}


@router.post("/channels/{login}/publications", status_code=201)
async def publish_command(
    request: Request, login: str, body: PublishBody, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    check_setting_role(request, caller, settings.login, "publish_min_role", "publish commands")
    return await publish_in(request, settings.channel_id, body, caller, settings.channel_id)


@router.delete("/channels/{login}/publications/{name}")
async def unpublish_command(request: Request, login: str, name: str, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    settings = _channel(request, login)
    check_setting_role(request, caller, settings.login, "publish_min_role", "unpublish commands")
    return await unpublish_in(request, settings.channel_id, name, caller)


class PackRef(BaseModel):
    """The caller's own pack, or someone's whose every command is shared (`@owner <pack>`)."""

    pack: str = Field(min_length=1, max_length=32)
    owner: str | None = Field(default=None, max_length=40)


async def _resolve_pack(request: Request, caller: Caller, name: str, owner: str | None) -> Pack:
    user_id, _ = _me(caller)
    packs = _packs(request)
    if owner is None:
        return await _own_pack(request, user_id, name, writable=False)
    user = await _state(request, "twitch").resolve_user(owner.lstrip("@"))
    pack = await packs.by_owner(user["id"], name) if user else None
    if pack is None:
        raise HTTPException(status_code=404, detail=f"@{owner} has no pack named {name}")
    if user and user["id"] != user_id:
        members = await packs.members(pack.id)
        if not members or not all(c.shareable for c in members):
            raise HTTPException(status_code=403, detail=f"@{owner} hasn't shared every command in {pack.name}")
    return pack


async def publish_pack_in(request: Request, scope: str, body: PackRef, caller: Caller) -> dict[str, Any]:
    user_id, _ = _me(caller)
    packs = _packs(request)
    pack = await _resolve_pack(request, caller, body.pack, body.owner)
    members = await packs.members(pack.id)
    if not members:
        raise HTTPException(status_code=400, detail=f"{pack.name} has no commands yet")
    try:
        await packs.publish(channel_id=scope, pack=pack, published_by=user_id, actor_via=caller.actor.via)
    except CustomCommandError as exc:
        raise _refused(exc) from exc
    return {
        "pack": pack.name,
        "commands": [c.name for c in members],
        "needs_grants": _missing_grants(request, scope, members),
    }


async def unpublish_pack_in(
    request: Request, scope: str, name: str, owner: str | None, caller: Caller
) -> dict[str, Any]:
    pack = await _resolve_pack(request, caller, name, owner)
    removed = await _packs(request).unpublish(
        channel_id=scope, pack=pack, actor_user_id=caller.actor.user_id, actor_via=caller.actor.via
    )
    if not removed:
        raise HTTPException(status_code=404, detail=f"{pack.name} isn't published there")
    return {"pack": pack.name, "removed": True}


@router.post("/channels/{login}/packs", status_code=201)
async def publish_pack(request: Request, login: str, body: PackRef, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    settings = _channel(request, login)
    check_setting_role(request, caller, settings.login, "publish_min_role", "publish packs")
    return await publish_pack_in(request, settings.channel_id, body, caller)


@router.delete("/channels/{login}/packs/{name}")
async def unpublish_pack(
    request: Request,
    login: str,
    name: str,
    owner: str | None = Query(default=None, max_length=40),
    caller: Caller = PERSONAL_WRITE,
) -> dict[str, Any]:
    settings = _channel(request, login)
    check_setting_role(request, caller, settings.login, "publish_min_role", "unpublish packs")
    return await unpublish_pack_in(request, settings.channel_id, name, owner, caller)


# ── write grants (`cc grant`, `cc revoke`) ──────────────────────────────────
def _channel_writes(request: Request, command: CustomCommand, prefix: str) -> list[str]:
    """The channel variables a body writes; published, each needs a grant (ADR-0010)."""
    runtime = _state(request, "runtime")
    try:
        node = parse(command.body, Context.BODY, runtime.parser_params(prefix))
    except ParseError:
        return []
    return sorted({f"{s.target.namespace}.{s.target.name}" for s in stores(node) if s.target.namespace in GRANTABLE})


def _missing_grants(request: Request, scope: str, commands: list[CustomCommand]) -> dict[str, list[str]]:
    """What each command writes that isn't granted yet, as chat's warning after a publish says."""
    access = _state(request, "variable_access")
    prefix = DEFAULT_PREFIX
    if scope != GLOBAL:
        settings = _policy(request).channel_settings(scope)
        prefix = settings.prefix if settings is not None else prefix
    wanted: dict[str, list[str]] = {}
    for command in commands:
        held = frozenset() if scope == GLOBAL else access.granted(scope, command.id)
        missing = [v for v in _channel_writes(request, command, prefix) if v not in held]
        if missing:
            wanted[command.name] = missing
    return wanted


async def _reachable(request: Request, settings: ChannelSettings, name: str) -> CustomCommand:
    """What the channel runs under that name, in the runtime's order: its publication, a global one, a pack."""
    found = await _commands(request).publication_in_scope(settings.channel_id, name)
    if found is not None:
        return found[1]
    in_pack = await _packs(request).find_in_scope(settings.channel_id, name)
    if in_pack is None:
        raise HTTPException(status_code=404, detail=f"{name} isn't published in {settings.login}")
    return in_pack[0]


@router.get("/channels/{login}/grants")
async def channel_grants(request: Request, login: str, caller: Caller = PERSONAL_READ) -> dict[str, Any]:
    """For each command the channel offers that writes channel variables: what it writes, and what it may."""
    settings = _channel(request, login)
    check_setting_role(request, caller, settings.login, "grant_min_role", "see variable grants")
    service, packs, access = _commands(request), _packs(request), _state(request, "variable_access")
    offered: dict[str, CustomCommand] = {}
    for scope in (GLOBAL, settings.channel_id):  # the channel's own win, as at run time
        for publication, command in await service.publications_in(scope):
            offered[publication.name] = command
    for _, pack in await packs.publications_in(settings.channel_id, include_global=True):
        for member in await packs.members(pack.id):
            offered.setdefault(member.name, member)
    rows = []
    for name, command in sorted(offered.items()):
        writes = _channel_writes(request, command, settings.prefix)
        granted = sorted(access.granted(settings.channel_id, command.id))
        if writes or granted:
            rows.append(
                {
                    "name": name,
                    "id": command.id,
                    "owner": command.owner_login,
                    "writes": writes,
                    "granted": granted,
                }
            )
    return {"channel": settings.login, "grants": rows}


async def _set_grant(
    request: Request, login: str, name: str, variable: str, granted: bool, caller: Caller
) -> dict[str, Any]:
    settings = _channel(request, login)
    check_setting_role(request, caller, settings.login, "grant_min_role", "grant variable writes")
    command = await _reachable(request, settings, name)
    try:
        await _state(request, "variable_access").set_grant(
            settings.channel_id,
            command.id,
            variable.lower(),
            granted,
            caller.actor.user_id,
            via=caller.actor.via,
        )
    except ValueError as exc:
        raise _refused(exc) from exc
    return {"name": name, "variable": variable.lower(), "granted": granted}


@router.put("/channels/{login}/grants/{name}/{variable}")
async def grant_write(
    request: Request, login: str, name: str, variable: str, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    """`cc grant`: this command may write this exact channel variable here, whatever its owner edits later."""
    return await _set_grant(request, login, name, variable, True, caller)


@router.delete("/channels/{login}/grants/{name}/{variable}")
async def revoke_write(
    request: Request, login: str, name: str, variable: str, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    return await _set_grant(request, login, name, variable, False, caller)
