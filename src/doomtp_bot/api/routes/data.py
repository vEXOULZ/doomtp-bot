"""`/api/v1`: reading and changing what the bot knows (architecture §11).

Nothing here talks to the database on its own. Every read comes from the same snapshot the runtime uses,
and every write goes through the same service a chat command would call, so the audit log records it the
same way, only with the caller's `via`.

Three ways in:
  * **no auth** for what the public pages already show: a channel's commands and its publications;
  * **`Authorization: Bearer dtb_…`** with the `read` or `write` scope, for scripts and dashboards;
  * **the session cookie**, so the admin UI can use these endpoints. Cookies are sent by the
    browser whether or not the page meant to, so a cookie-authenticated *write* also needs the session's
    CSRF token in `X-CSRF-Token`. A key doesn't: it is never sent automatically.

A moderator's session reaches the `READ`/`WRITE` routes of its own channels only; `ADMIN_*` routes are for
admins (`api/access.py`, ADR-0017). Writes are audited as the caller: `via="api"`, or `via="web"` with the
user's id for a signed-in user.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from doomtp_bot.api.access import (
    ADMIN_READ,
    ADMIN_WRITE,
    MODERATOR_SETTABLE,
    READ,
    WRITE,
    WRITE_OWN,
    Caller,
    check_area,
)
from doomtp_bot.audit.log import read_audit
from doomtp_bot.chatlog import queries
from doomtp_bot.core.channels import ChannelBanned
from doomtp_bot.customcmds.packs import custom_modules
from doomtp_bot.customcmds.params import to_params
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.filters.matcher import FilterError
from doomtp_bot.filters.service import FilterService
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.policy.snapshot import ChannelSettings
from doomtp_bot.runtime.spec import CommandSpec, LogLevel
from doomtp_bot.runtime.variables import Space
from doomtp_bot.triggers.service import TRIGGER_TYPES, TriggerError, TriggerService

router = APIRouter(prefix="/api/v1", tags=["data"])

MAX_ROWS = 500
SETTABLE = (
    "prefix", "quiet_errors", "cc_edit_notice", "log_enabled", "history_backfill", "reply_hold_ms", "timezone",
    "automod_action", "automod_timeout_s", "channel_var_write_role", "grant_min_role",
    "publish_min_role", "create_min_role", "var_admin_role",
)  # fmt: skip


# ── plumbing ────────────────────────────────────────────────────────────────
def _state(request: Request, name: str) -> Any:
    found = getattr(request.app.state, name, None)
    if found is None:
        raise HTTPException(status_code=503, detail=f"{name} isn't available")
    return found


def _policy(request: Request) -> PolicyService:
    return _state(request, "policy")  # type: ignore[no-any-return]


def _channel(request: Request, login: str) -> ChannelSettings:
    settings = _policy(request).channel_by_login(login)
    if settings is not None:
        return settings
    raise HTTPException(status_code=404, detail=f"no channel named {login}")


# ── channels ────────────────────────────────────────────────────────────────


def _channel_json(settings: ChannelSettings) -> dict[str, Any]:
    return {
        "channel_id": settings.channel_id,
        "login": settings.login,
        "active": settings.active,
        "status": settings.status,
        # Left after Twitch refused a message with 403. Only a join with `rejoin` brings it back.
        "banned": settings.status == "banned",
        "tier": settings.tier,
        "capabilities": sorted(settings.capabilities),
        "prefix": settings.prefix,
        "timezone": settings.timezone,
        "log_enabled": settings.log_enabled,
        "history_backfill": settings.history_backfill,
        "quiet_errors": settings.quiet_errors,
        "cc_edit_notice": settings.cc_edit_notice,
        "reply_hold_ms": settings.reply_hold_ms,
        "automod": {"action": settings.automod_action, "timeout_s": settings.automod_timeout_s},
        "roles": {
            "channel_var_write": settings.channel_var_write_role,
            "grant_min": settings.grant_min_role,
            "publish_min": settings.publish_min_role,
            "create_min": settings.create_min_role,
            "var_admin": settings.var_admin_role,
        },
    }


class ChannelPatch(BaseModel):
    prefix: str | None = Field(default=None, max_length=16)
    quiet_errors: bool | None = None
    cc_edit_notice: bool | None = None
    log_enabled: bool | None = None
    history_backfill: bool | None = None
    reply_hold_ms: int | None = Field(default=None, ge=0, le=5000)
    timezone: str | None = None
    automod_action: Literal["off", "delete", "timeout"] | None = None
    automod_timeout_s: int | None = Field(default=None, ge=1, le=1_209_600)
    channel_var_write_role: str | None = None
    grant_min_role: str | None = None
    publish_min_role: str | None = None
    create_min_role: str | None = None
    var_admin_role: str | None = None


class JoinRequest(BaseModel):
    login: str = Field(min_length=1, max_length=40)
    # Needed to come back to a channel the bot left because it was banned there (architecture §10).
    rejoin: bool = False


class Enabled(BaseModel):
    enabled: bool


@router.get("/channels")
async def list_channels(request: Request, caller: Caller = READ) -> dict[str, Any]:
    channels = [c for c in _policy(request).channels() if caller.manages(c.login)]
    return {"channels": [_channel_json(c) for c in sorted(channels, key=lambda c: c.login)]}


@router.get("/channels/{login}")
async def get_channel(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    return _channel_json(_channel(request, login))


@router.post("/channels", status_code=201)
async def join_channel(request: Request, body: JoinRequest, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    """Join a channel by login. The same path `!join` takes, including the EventSub subscriptions."""
    channels, twitch = _state(request, "channels"), _state(request, "twitch")
    user = await twitch.resolve_user(body.login)
    if user is None:
        raise HTTPException(status_code=404, detail=f"no Twitch user named {body.login}")
    try:
        failed = await channels.join(user["id"], user["name"], caller.actor, rejoin=body.rejoin)
    except ChannelBanned as exc:
        raise HTTPException(status_code=409, detail=f"{exc}; send rejoin=true to join anyway") from exc
    return {"login": user["name"], "channel_id": user["id"], "failed_subscriptions": failed}


@router.delete("/channels/{login}")
async def part_channel(request: Request, login: str, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    settings = _channel(request, login)
    await _state(request, "channels").part(settings.channel_id, caller.actor)
    return {"login": settings.login, "status": "parted"}


@router.patch("/channels/{login}")
async def patch_channel(
    request: Request, login: str, body: ChannelPatch, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    policy = _policy(request)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in SETTABLE}
    if not changes:
        raise HTTPException(status_code=400, detail=f"nothing to change; fields: {', '.join(SETTABLE)}")
    refused = sorted(set(changes) - MODERATOR_SETTABLE) if not caller.is_admin else []
    if refused:
        raise HTTPException(status_code=403, detail=f"only an admin can change {', '.join(refused)}")

    async def write(repo: Any) -> None:
        for column, value in changes.items():
            await repo.set_channel_field(settings.channel_id, column, value, caller.actor)

    await policy.mutate(write)  # one snapshot reload for the whole patch
    return _channel_json(_channel(request, settings.login))


@router.get("/channels/{login}/modules")
async def list_modules(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    """Each module, whether it is on here, and whether it can be turned off at all.

    `kind` says where it comes from: `builtin`, a `pack` published here or globally (its name is its
    module name, ADR-0012), or `custom`, which holds the commands published one by one and appears when
    there are any. `enabled` follows the rules chat applies: this channel's toggle, then the global one,
    then on. Turn any of them off with `PUT …/modules/{module}`, as `!module disable` does in chat.
    """
    settings, policy = _channel(request, login), _policy(request)
    specs: dict[str, tuple[CommandSpec, str]] = {
        c.spec.module: (c.spec, "builtin") for c in _state(request, "runtime").registry.all()
    }
    services = request.app.state
    found = await custom_modules(
        settings.channel_id, getattr(services, "packs", None), getattr(services, "customcmds", None)
    )
    for name, kind in found.items():
        specs.setdefault(name, (CommandSpec(name="", module=name, summary=""), kind))
    return {
        "modules": [
            {
                "module": m,
                "enabled": policy.is_enabled(settings.channel_id, s),
                "toggleable": s.toggleable,
                "kind": kind,
            }
            for m, (s, kind) in sorted(specs.items())
        ]
    }


class IgnoreBody(BaseModel):
    login: str = Field(min_length=1, max_length=40)
    everywhere: bool = False  # every channel, as `ignore add <user> global` does in chat
    reason: str | None = Field(default=None, max_length=200)


async def _login_of(request: Request, user_id: str | None, known: dict[str, str]) -> str | None:
    """A user id's login: from what the bot already stores, else from Twitch. None when neither knows."""
    if user_id is None:
        return None
    if user_id not in known:
        twitch = getattr(request.app.state, "twitch", None)
        lookup = getattr(twitch, "login_for", None)
        try:
            found = await lookup(user_id) if lookup is not None else None
        except Exception:  # Twitch being down must not take the list with it
            found = None
        if found is not None:
            known[user_id] = found
    return known.get(user_id)


async def _ignored_json(request: Request, scope: str) -> list[dict[str, Any]]:
    policy = _policy(request)
    known = {c.channel_id: c.login for c in policy.channels()}
    for s in (scope, GLOBAL):
        known.update({e.user_id: e.user_login for e in policy.ignore_entries(s) if e.user_login})
    return [
        {
            "user_id": e.user_id,
            "login": e.user_login,
            "reason": e.reason,
            "added_by": e.added_by,
            "added_by_login": await _login_of(request, e.added_by, known),
            "added_at": e.added_at,
        }
        for e in sorted(policy.ignore_entries(scope), key=lambda e: (e.user_login or "", e.user_id))
    ]


@router.get("/channels/{login}/ignored")
async def ignored_users(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    """Who the bot ignores here, and who it ignores in every channel: each as `{user_id, login, reason,
    added_by, added_by_login, added_at}`, `added_at` in epoch ms. `added_by` is the user id of whoever
    set it (their own, after `ignore me` in chat), or null when it came from the API or the admin UI.
    Changed from chat (`ignore`, `ignore me`, `unignore me`) or with the two endpoints below."""
    settings = _channel(request, login)
    return {
        "ignored": await _ignored_json(request, settings.channel_id),
        "ignored_everywhere": await _ignored_json(request, GLOBAL),
    }


@router.post("/channels/{login}/ignored", status_code=201)
async def add_ignored(
    request: Request, login: str, body: IgnoreBody, caller: Caller = WRITE
) -> dict[str, Any]:
    """Ignore a user here, or everywhere with `everywhere: true`. Audited as `ignore.add`, like chat."""
    settings, policy = _channel(request, login), _policy(request)
    user = await _state(request, "twitch").resolve_user(body.login)
    if user is None:
        raise HTTPException(status_code=404, detail=f"no Twitch user named {body.login}")
    scope = GLOBAL if body.everywhere else settings.channel_id
    await policy.mutate(
        lambda repo: repo.set_ignored(scope, user["id"], user["name"], True, caller.actor, reason=body.reason)
    )
    return {"user_id": user["id"], "login": user["name"], "everywhere": body.everywhere}


@router.delete("/channels/{login}/ignored/{user_id}")
async def remove_ignored(
    request: Request,
    login: str,
    user_id: str,
    everywhere: bool = Query(default=False, description="lift an ignore set for every channel instead"),
    caller: Caller = WRITE_OWN,
) -> dict[str, Any]:
    """Stop ignoring a user here (or everywhere). Audited as `ignore.remove`, like chat.

    A signed-in user may also lift an ignore they set on themselves (`ignore me`), in any channel."""
    own = caller.user_id == user_id and not everywhere
    if not own:
        check_area(caller, "channel", login)
    settings, policy = _channel(request, login), _policy(request)
    scope = GLOBAL if everywhere else settings.channel_id
    entry = policy.ignore_entry(scope, user_id)
    if own and not caller.manages(login) and (entry is None or entry.added_by != user_id):
        raise HTTPException(status_code=403, detail="only a moderator can lift an ignore someone else set")
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"{user_id} isn't ignored {'everywhere' if everywhere else 'here'}"
        )
    await policy.mutate(
        lambda repo: repo.set_ignored(scope, user_id, entry.user_login or "", False, caller.actor)
    )
    return {"user_id": user_id, "removed": True, "everywhere": everywhere}


@router.put("/channels/{login}/modules/{module}")
async def set_module(
    request: Request, login: str, module: str, body: Enabled, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    policy = _policy(request)
    await policy.mutate(
        lambda repo: repo.set_module_toggle(settings.channel_id, module, body.enabled, caller.actor)
    )
    return {"module": module, "enabled": body.enabled}


# ── commands ────────────────────────────────────────────────────────────────
class CommandRule(BaseModel):
    enabled: bool | None = None
    required_role: str | None = None
    log_level: Literal["off", "errors", "output", "invocations", "all"] | None = None


@router.get("/channels/{login}/commands")
async def channel_commands(request: Request, login: str) -> dict[str, Any]:
    """What this channel's chat can actually run, with the rules that apply here. Public, like the page."""
    settings = _channel(request, login)
    policy, runtime = _policy(request), _state(request, "runtime")
    listing = []
    for command in runtime.registry.all():
        spec = command.spec
        required, allowed = policy.required_role(settings.channel_id, spec)
        listing.append(
            {
                "name": spec.name,
                "module": spec.module,
                "summary": spec.summary,
                "enabled": policy.is_enabled(settings.channel_id, spec),
                "required_role": required,
                "allowed_roles": list(allowed) if allowed else None,
                "cooldowns": {
                    role: {"tier_s": cd.tier_s, "user_s": cd.user_s}
                    for role, cd in policy.cooldown_rules(settings.channel_id, spec).items()
                },
                "requires": list(spec.requires),
                "missing": sorted(set(spec.requires) - set(settings.capabilities)),
            }
        )
    return {"channel": settings.login, "prefix": settings.prefix, "commands": listing}


@router.patch("/channels/{login}/commands/{name}")
async def patch_command(
    request: Request, login: str, name: str, body: CommandRule, caller: Caller = WRITE
) -> dict[str, Any]:
    settings, policy = _channel(request, login), _policy(request)
    runtime = _state(request, "runtime")
    if runtime.registry.get(name) is None:
        raise HTTPException(status_code=404, detail=f"no command named {name}")
    fields = body.model_dump(exclude_unset=True)
    if "enabled" in fields or "log_level" in fields:
        await policy.mutate(
            lambda repo: repo.set_command_toggle(
                settings.channel_id,
                name,
                caller.actor,
                enabled=fields.get("enabled"),
                log_level=fields.get("log_level"),
                clear_enabled="enabled" in fields and fields["enabled"] is None,
            )
        )
    if "required_role" in fields:
        role = fields["required_role"]
        if role is not None and policy.rank_of(settings.channel_id, role) is None:
            raise HTTPException(status_code=400, detail=f"no role named {role}")
        await policy.mutate(
            lambda repo: repo.set_command_rule(settings.channel_id, name, role, None, caller.actor)
        )
    required, allowed = policy.required_role(settings.channel_id, runtime.registry.get(name).spec)
    return {
        "name": name,
        "enabled": policy.is_enabled(settings.channel_id, runtime.registry.get(name).spec),
        "required_role": required,
        "allowed_roles": list(allowed) if allowed else None,
    }


# ── filters ─────────────────────────────────────────────────────────────────
class FilterBody(BaseModel):
    pattern: str = Field(min_length=1, max_length=200)
    kind: Literal["word", "wildcard", "regex", "allow"] = "word"
    action: Literal["mask", "replace", "tag", "block"] = "mask"
    category: str = ""
    replacement: str = ""


@router.get("/channels/{login}/filters")
async def list_filters(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    settings = _channel(request, login)
    filters: FilterService = _state(request, "filters")
    return {
        "filters": [
            {
                "id": e.id,
                "pattern": e.pattern,
                "kind": e.kind,
                "action": e.action,
                "category": e.category,
                "replacement": e.replacement,
                "enabled": e.enabled,
                "global": e.channel_id == GLOBAL,
            }
            for e in filters.entries_for(settings.channel_id)
        ]
    }


@router.post("/channels/{login}/filters", status_code=201)
async def add_filter(
    request: Request, login: str, body: FilterBody, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    filters: FilterService = _state(request, "filters")
    try:
        entry = await filters.add(
            channel_id=settings.channel_id,
            pattern=body.pattern,
            kind=body.kind,
            action=body.action,
            category=body.category,
            replacement=body.replacement,
            actor_user_id=caller.actor.user_id,
            via=caller.actor.via,
        )
    except FilterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": entry.id, "pattern": entry.pattern, "kind": entry.kind, "action": entry.action}


@router.patch("/channels/{login}/filters/{entry_id}")
async def set_filter_enabled(
    request: Request, login: str, entry_id: int, body: Enabled, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    filters: FilterService = _state(request, "filters")
    changed = await filters.set_enabled(
        channel_id=settings.channel_id,
        entry_id=entry_id,
        enabled=body.enabled,
        actor_user_id=caller.actor.user_id,
        via=caller.actor.via,
    )
    if not changed:
        raise HTTPException(status_code=404, detail=f"no filter {entry_id} here")
    return {"id": entry_id, "enabled": body.enabled}


@router.delete("/channels/{login}/filters/{entry_id}")
async def remove_filter(
    request: Request, login: str, entry_id: int, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    filters: FilterService = _state(request, "filters")
    if not await filters.remove(
        channel_id=settings.channel_id,
        entry_id=entry_id,
        actor_user_id=caller.actor.user_id,
        via=caller.actor.via,
    ):
        raise HTTPException(status_code=404, detail=f"no filter {entry_id} here")
    return {"id": entry_id, "removed": True}


# ── triggers ────────────────────────────────────────────────────────────────
class TriggerBody(BaseModel):
    type: str
    expr: str = Field(min_length=1)
    match: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    run_as_rank: int = Field(default=80, ge=0, le=10_000)
    log_level: Literal["off", "errors", "output", "invocations", "all"] = "output"


def _trigger_json(trigger: Any) -> dict[str, Any]:
    return {
        "id": trigger.id,
        "type": trigger.type,
        "expr": trigger.expr,
        "match": trigger.match,
        "schedule": trigger.schedule,
        "enabled": trigger.enabled,
        "run_as_rank": trigger.run_as_rank,
    }


@router.get("/channels/{login}/triggers")
async def list_triggers(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    settings = _channel(request, login)
    triggers: TriggerService = _state(request, "triggers")
    return {"triggers": [_trigger_json(t) for t in triggers.in_channel(settings.channel_id)]}


@router.post("/channels/{login}/triggers", status_code=201)
async def add_trigger(
    request: Request, login: str, body: TriggerBody, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    triggers: TriggerService = _state(request, "triggers")
    if body.type not in TRIGGER_TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of: {', '.join(TRIGGER_TYPES)}")
    try:
        trigger = await triggers.add(
            channel_id=settings.channel_id,
            type_=body.type,
            expr=body.expr,
            match=body.match,
            schedule=body.schedule,
            run_as_rank=body.run_as_rank,
            log_level=LogLevel(body.log_level),
            created_by=None,
            prefix=settings.prefix,
            via=caller.actor.via,
        )
    except TriggerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _trigger_json(trigger)


@router.patch("/channels/{login}/triggers/{trigger_id}")
async def set_trigger_enabled(
    request: Request, login: str, trigger_id: int, body: Enabled, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    triggers: TriggerService = _state(request, "triggers")
    changed = await triggers.set_enabled(
        channel_id=settings.channel_id,
        trigger_id=trigger_id,
        enabled=body.enabled,
        actor_user_id=caller.actor.user_id,
        via=caller.actor.via,
    )
    if not changed:
        raise HTTPException(status_code=404, detail=f"no trigger {trigger_id} here")
    return {"id": trigger_id, "enabled": body.enabled}


@router.delete("/channels/{login}/triggers/{trigger_id}")
async def remove_trigger(
    request: Request, login: str, trigger_id: int, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    triggers: TriggerService = _state(request, "triggers")
    if not await triggers.remove(
        channel_id=settings.channel_id,
        trigger_id=trigger_id,
        actor_user_id=caller.actor.user_id,
        via=caller.actor.via,
    ):
        raise HTTPException(status_code=404, detail=f"no trigger {trigger_id} here")
    return {"id": trigger_id, "removed": True}


# ── custom commands (ADR-0009 action item 5) ────────────────────────────────
def _custom_json(command: Any) -> dict[str, Any]:
    return {
        "id": command.id,
        "name": command.name,
        "owner": command.owner_login,
        "summary": command.summary,
        "body": command.body,
        "version": command.version,
        "shareable": command.shareable,
        "params": [
            {
                "position": p.position,
                "name": p.name,
                "type": p.type,
                "required": p.required,
                "description": p.description,
                "choices": list(p.choices),
            }
            for p in to_params(command.params)
        ],
    }


@router.get("/custom-commands")
async def custom_commands(
    request: Request, owner: str | None = Query(default=None, max_length=40)
) -> dict[str, Any]:
    """Public: what is published everywhere (ADR-0012), or one owner's shared commands."""
    service: CustomCommandService = _state(request, "customcmds")
    if owner is None:
        published = await service.publications_in(GLOBAL)
        return {"commands": [{**_custom_json(c), "published_as": p.name} for p, c in published]}
    twitch = _state(request, "twitch")
    user = await twitch.resolve_user(owner)
    if user is None:
        raise HTTPException(status_code=404, detail=f"no Twitch user named {owner}")
    owned = await service.owned_by(user["id"])
    return {"commands": [_custom_json(c) for c in owned if c.shareable]}


@router.get("/channels/{login}/publications")
async def publications(request: Request, login: str) -> dict[str, Any]:
    """Public: the custom commands this channel offers, and who wrote them."""
    settings = _channel(request, login)
    service: CustomCommandService = _state(request, "customcmds")
    found = await service.publications_in(settings.channel_id)
    return {
        "channel": settings.login,
        "publications": [
            {
                **_custom_json(command),
                "published_as": publication.name,
                "status": publication.status,
                "required_role": publication.required_role,
            }
            for publication, command in found
        ],
    }


@router.patch("/channels/{login}/publications/{name}")
async def set_publication(
    request: Request, login: str, name: str, body: Enabled, caller: Caller = ADMIN_WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    service: CustomCommandService = _state(request, "customcmds")
    changed = await service.set_publication_status(
        channel_id=settings.channel_id,
        name=name,
        status="active" if body.enabled else "disabled",
        actor_user_id=caller.actor.user_id,
        actor_via=caller.actor.via,
    )
    if not changed:
        raise HTTPException(status_code=404, detail=f"{name} isn't published here")
    return {"name": name, "enabled": body.enabled}


# ── variables, logs and the audit trail ─────────────────────────────────────
@router.get("/channels/{login}/variables")
async def channel_variables(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    """The channel's own variables. Writing them belongs to the runtime, where the access rules live."""
    settings = _channel(request, login)
    store = _state(request, "variable_store")
    entries = await store.entries(Space("channel", settings.channel_id, "", ""))
    return {
        "variables": [
            {"name": e.key.name, "value": e.value, "updated_at": e.updated_at, "updated_by": e.updated_by}
            for e in entries
        ]
    }


@router.get("/channels/{login}/runs")
async def command_runs(
    request: Request,
    login: str,
    limit: int = Query(default=50, ge=1, le=MAX_ROWS),
    caller: Caller = ADMIN_READ,
) -> dict[str, Any]:
    settings = _channel(request, login)
    conn = _state(request, "chatlog_db")
    async with await conn.execute(
        "SELECT user_id, trigger_type, expr, code, message, duration_ms, cancelled_reason, at"
        " FROM command_runs WHERE channel_id = %s ORDER BY at DESC LIMIT %s",
        (settings.channel_id, limit),
    ) as cur:
        return {"runs": [dict(row) for row in await cur.fetchall()]}


@router.get("/channels/{login}/messages")
async def search_messages(
    request: Request,
    login: str,
    q: str = Query(min_length=1, max_length=queries.MAX_QUERY_CHARS),
    limit: int = Query(default=50, ge=1, le=MAX_ROWS),
    caller: Caller = ADMIN_READ,
) -> dict[str, Any]:
    """Full-text search over the channel's log (architecture §3.3)."""
    settings = _channel(request, login)
    rows = await queries.search_messages(_state(request, "chatlog_db"), settings.channel_id, q, limit=limit)
    return {"query": q, "messages": rows}


@router.get("/audit")
async def audit(
    request: Request,
    channel: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=MAX_ROWS),
    caller: Caller = READ,
) -> dict[str, Any]:
    conn = _state(request, "bot_db")
    if channel is not None:
        check_area(caller, "channel", channel)
        found = await read_audit(conn, channel_id=_channel(request, channel).channel_id, limit=limit)
        return {"entries": found}
    if caller.is_admin:
        return {"entries": await read_audit(conn, limit=limit)}
    ids = [c.channel_id for c in _policy(request).channels() if caller.manages(c.login)]
    return {"entries": await read_audit(conn, channel_ids=ids, limit=limit)}
