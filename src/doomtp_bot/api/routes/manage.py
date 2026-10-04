"""`/api/v1/channels/{login}/…`: a channel's settings that were only reachable from chat (ADR-0026).

Custom roles (`!role`), the replies sent on a cooldown or a denial (`!callback`), the readouts' own
wording (`!customecho`), writing the channel's variables (`!var set channel.…`), and dry runs of the word
filter, the automod and the listeners. Each route checks what the chat command checks, through the same
service, so the audit log reads the same, only with the caller's `via`.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from doomtp_bot.api.access import ADMIN_WRITE, PERSONAL_WRITE, READ, WRITE, Caller, check_setting_role
from doomtp_bot.api.routes.data import _channel, _policy, _state, _trigger_json
from doomtp_bot.clock import now_ms
from doomtp_bot.lang import SYNTAX_VERSION
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import Context, parse, parse_template
from doomtp_bot.moderation.automod import MAX_TIMEOUT_S
from doomtp_bot.policy.roles import (
    BUILTIN_RANKS,
    CUSTOM_RANK_MAX,
    CUSTOM_RANK_MIN,
    GLOBAL,
    Role,
    can_manage_role,
)
from doomtp_bot.policy.snapshot import ChannelSettings
from doomtp_bot.runtime.context import ExecContext
from doomtp_bot.runtime.values import MISSING
from doomtp_bot.runtime.variables import VariableError, WriteOp, key_for

router = APIRouter(prefix="/api/v1", tags=["manage"])

# The same rules as the chat commands (modules/core_admin.py).
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
SCOPE_RE = re.compile(r"^(channel|module:[a-z0-9_]+|command:[a-z0-9][a-z0-9_-]*)$")
ECHO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CallbackKind = Literal["on_cooldown", "on_denied"]


def _filter_check(request: Request, channel_id: str, *texts: str) -> None:
    """Stored text is read out later, so the channel's filter sees it first, as `reject_filtered` does."""
    filters = getattr(request.app.state, "filters", None)
    hits = filters.rejects_any(channel_id, *texts) if filters is not None else []
    if hits:
        raise HTTPException(status_code=400, detail=f"the filter rejects that: {', '.join(hits)}")


def _is_broadcaster(caller: Caller, settings: ChannelSettings) -> bool:
    return caller.user_id is not None and caller.user_id == settings.channel_id


# ── dry runs ────────────────────────────────────────────────────────────────
class TextBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@router.post("/channels/{login}/filters/test")
async def test_filter(request: Request, login: str, body: TextBody, caller: Caller = READ) -> dict[str, Any]:
    """What the word filter does to a message, and what the automod would do to a chatter who sent it."""
    settings = _channel(request, login)
    result = _state(request, "filters").check(settings.channel_id, body.text)
    automod: dict[str, Any] | None = None
    if settings.automod_action != "off" and result.blocked:
        automod = {"action": settings.automod_action}
        if settings.automod_action == "timeout":
            automod["seconds"] = min(max(settings.automod_timeout_s, 1), MAX_TIMEOUT_S)
    return {
        "text": result.text,
        "blocked": result.blocked,
        "changed": result.changed,
        "patterns": result.patterns(),
        # For a chatter below moderator, and only while the bot is a moderator here.
        "automod": automod,
        "automod_able": "moderate" in settings.capabilities,
    }


@router.post("/channels/{login}/triggers/test")
async def test_listeners(request: Request, login: str, body: TextBody, caller: Caller = READ) -> dict[str, Any]:
    """Which listeners a chat line would set off, and what each would capture. Nothing runs."""
    settings = _channel(request, login)
    matched = _state(request, "triggers").listeners_matching(settings.channel_id, body.text)
    return {"matches": [{"trigger": _trigger_json(t), "fields": fields} for t, fields in matched]}


# ── roles (`!role`) ─────────────────────────────────────────────────────────
class RoleBody(BaseModel):
    name: str = Field(min_length=2, max_length=32)
    rank: int = Field(ge=CUSTOM_RANK_MIN, le=CUSTOM_RANK_MAX)


class MemberBody(BaseModel):
    duration_s: int | None = Field(default=None, ge=60, le=366 * 86_400)  # none: until removed


def _manageable(role: Role, settings: ChannelSettings, caller: Caller, rank: int) -> bool:
    return not role.builtin and can_manage_role(
        rank,
        role.rank,
        actor_is_broadcaster=_is_broadcaster(caller, settings),
        role_is_channel=role.channel_id == settings.channel_id,
    )


def _role_here(request: Request, settings: ChannelSettings, name: str) -> Role:
    role = _policy(request).role_named(settings.channel_id, name.lower())
    if role is None:
        raise HTTPException(status_code=404, detail=f"no role named {name}")
    return role


@router.get("/channels/{login}/roles")
async def list_roles(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    """The built-in ranks, then the custom roles (the channel's and the bot-wide ones) with who holds
    them. `manageable` says whether the caller may give, take or delete it, as `!role` decides."""
    settings, policy = _channel(request, login), _policy(request)
    rank = caller.rank_in(policy, settings.login)
    custom = {**policy.roles_in(GLOBAL), **policy.roles_in(settings.channel_id)}
    roles = []
    for role in sorted(custom.values(), key=lambda r: -r.rank):
        members = await policy.members_of(role)
        roles.append(
            {
                "name": role.name,
                "rank": role.rank,
                "global": role.channel_id == GLOBAL,
                "manageable": _manageable(role, settings, caller, rank),
                "members": [{"user_id": u, "login": lg, "expires_at": exp} for u, lg, exp in members],
            }
        )
    return {
        "builtin": [{"name": n, "rank": r} for n, r in sorted(BUILTIN_RANKS.items(), key=lambda i: i[1])],
        "roles": roles,
        "your_rank": rank,
    }


@router.post("/channels/{login}/roles", status_code=201)
async def create_role(request: Request, login: str, body: RoleBody, caller: Caller = WRITE) -> dict[str, Any]:
    settings, policy = _channel(request, login), _policy(request)
    name = body.name.lower()
    if not ROLE_NAME_RE.match(name) or name in BUILTIN_RANKS:
        raise HTTPException(status_code=400, detail="role names: lowercase letters, digits, _ (not a built-in role)")
    if name in policy.roles_in(settings.channel_id):
        raise HTTPException(status_code=409, detail=f"role {name} already exists")
    rank = caller.rank_in(policy, settings.login)
    if not can_manage_role(
        rank, body.rank, actor_is_broadcaster=_is_broadcaster(caller, settings), role_is_channel=True
    ):
        raise HTTPException(status_code=403, detail="you can only create roles ranked below your own")
    await policy.mutate(lambda repo: repo.create_role(settings.channel_id, name, body.rank, caller.actor))
    return {"name": name, "rank": body.rank}


@router.delete("/channels/{login}/roles/{name}")
async def delete_role(request: Request, login: str, name: str, caller: Caller = WRITE) -> dict[str, Any]:
    settings, policy = _channel(request, login), _policy(request)
    role = _role_here(request, settings, name)
    rank = caller.rank_in(policy, settings.login)
    if role.channel_id != settings.channel_id or not _manageable(role, settings, caller, rank):
        raise HTTPException(status_code=403, detail=f"you can't delete {role.name}")
    await policy.mutate(lambda repo: repo.delete_role(role.id, caller.actor))
    return {"name": role.name, "removed": True}


@router.put("/channels/{login}/roles/{name}/members/{user}")
async def add_role_member(
    request: Request, login: str, name: str, user: str, body: MemberBody, caller: Caller = WRITE
) -> dict[str, Any]:
    """Give a role to a user (by login), for `duration_s` or until removed."""
    settings, policy = _channel(request, login), _policy(request)
    role = _role_here(request, settings, name)
    if not _manageable(role, settings, caller, caller.rank_in(policy, settings.login)):
        raise HTTPException(status_code=403, detail=f"you can't manage {role.name}")
    found = await _state(request, "twitch").resolve_user(user)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no Twitch user named {user}")
    expires = None if body.duration_s is None else now_ms() + body.duration_s * 1000
    await policy.mutate(
        lambda repo: repo.add_member(
            role.id, settings.channel_id, role.name, found["id"], found["name"], expires, caller.actor
        )
    )
    return {"role": role.name, "user_id": found["id"], "login": found["name"], "expires_at": expires}


@router.delete("/channels/{login}/roles/{name}/members/{user}")
async def remove_role_member(
    request: Request, login: str, name: str, user: str, caller: Caller = WRITE
) -> dict[str, Any]:
    settings, policy = _channel(request, login), _policy(request)
    role = _role_here(request, settings, name)
    if not _manageable(role, settings, caller, caller.rank_in(policy, settings.login)):
        raise HTTPException(status_code=403, detail=f"you can't manage {role.name}")
    found = await _state(request, "twitch").resolve_user(user)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no Twitch user named {user}")
    removed = await policy.mutate(
        lambda repo: repo.remove_member(role.id, settings.channel_id, role.name, found["id"], caller.actor)
    )
    if not removed:
        raise HTTPException(status_code=404, detail=f"{found['name']} doesn't have {role.name}")
    return {"role": role.name, "user_id": found["id"], "removed": True}


# ── callbacks (`!callback`) ─────────────────────────────────────────────────
class ExprBody(BaseModel):
    expr: str = Field(min_length=1, max_length=2000)


def _callback_scope(kind: str, scope: str) -> str:
    scope = scope.lower()
    if kind not in ("on_cooldown", "on_denied") or not SCOPE_RE.match(scope):
        raise HTTPException(status_code=400, detail="scope: channel, module:<name> or command:<name>")
    return scope


@router.get("/channels/{login}/callbacks")
async def list_callbacks(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    """What chat hears instead of the default when a command is on cooldown or denied, per scope."""
    settings = _channel(request, login)
    found = _policy(request).callbacks_in(settings.channel_id)
    return {
        "callbacks": [{"scope": scope, "kind": kind, "expr": expr} for (scope, kind), expr in sorted(found.items())]
    }


@router.put("/channels/{login}/callbacks/{kind}/{scope}")
async def set_callback(
    request: Request, login: str, kind: CallbackKind, scope: str, body: ExprBody, caller: Caller = WRITE
) -> dict[str, Any]:
    settings, policy = _channel(request, login), _policy(request)
    scope = _callback_scope(kind, scope)
    runtime = _state(request, "runtime")
    try:
        parse(body.expr, Context.CALLBACK, runtime.parser_params(settings.prefix))
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await policy.mutate(
        lambda repo: repo.set_callback(settings.channel_id, scope, kind, body.expr, SYNTAX_VERSION, caller.actor)
    )
    return {"scope": scope, "kind": kind, "expr": body.expr}


@router.delete("/channels/{login}/callbacks/{kind}/{scope}")
async def clear_callback(
    request: Request, login: str, kind: CallbackKind, scope: str, caller: Caller = WRITE
) -> dict[str, Any]:
    settings, policy = _channel(request, login), _policy(request)
    scope = _callback_scope(kind, scope)
    if (scope, kind) not in policy.callbacks_in(settings.channel_id):
        raise HTTPException(status_code=404, detail=f"no {kind} reply for {scope}")
    await policy.mutate(
        lambda repo: repo.set_callback(settings.channel_id, scope, kind, None, SYNTAX_VERSION, caller.actor)
    )
    return {"scope": scope, "kind": kind, "removed": True}


# ── the channel's variables: customecho and `!var set channel.…` ────────────
def web_context(request: Request, settings: ChannelSettings, caller: Caller) -> ExecContext:
    """A run context for a write from the web: the caller as the invoker, as if they had typed it."""
    policy, runtime = _policy(request), _state(request, "runtime")
    invoker = None
    if caller.user_id is not None:
        role = caller.channel_role(policy, settings.login)
        badges = frozenset({role}) if role is not None else frozenset[str]()
        invoker = policy.build_chatter(settings.channel_id, caller.user_id, caller.login or "", badges=badges)
    channel = policy.channel_info(settings.channel_id, settings.login)
    return runtime.make_context(channel=channel, invoker=invoker, trigger_type=caller.actor.via)  # type: ignore[no-any-return]


async def commit(request: Request, ctx: ExecContext, op: WriteOp) -> None:
    try:
        await _state(request, "variable_store").commit([op], ctx)
    except VariableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _var_key(ctx: ExecContext, namespace: str, name: str) -> Any:
    try:
        return key_for(ctx, namespace, name)
    except VariableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _customecho(request: Request, settings: ChannelSettings, caller: Caller) -> tuple[Any, Any, Any]:
    ctx = web_context(request, settings, caller)
    key = _var_key(ctx, "channel", "customecho")
    current = await _state(request, "variable_store").get(key)
    return ctx, key, current


# ADR-0019
@router.get("/channels/{login}/customecho")
async def list_customecho(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    """The channel's own wording for readout commands, by command."""
    settings = _channel(request, login)
    _, _, current = await _customecho(request, settings, caller)
    found = current if isinstance(current, dict) else {}
    return {"customecho": [{"command": k, "template": v} for k, v in sorted(found.items())]}


@router.put("/channels/{login}/customecho/{name}")
async def set_customecho(
    request: Request, login: str, name: str, body: TextBody, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    name = name.lower().removeprefix(settings.prefix)
    if not ECHO_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"no readout named {name}")
    template = body.text.strip()
    try:
        parse_template(template)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _filter_check(request, settings.channel_id, template)
    ctx, key, current = await _customecho(request, settings, caller)
    if current is not MISSING and not isinstance(current, dict):
        raise HTTPException(status_code=409, detail="channel.customecho isn't a map: delete that variable first")
    await commit(request, ctx, WriteOp("set", key, template, (name,)))
    return {"command": name, "template": template}


@router.delete("/channels/{login}/customecho/{name}")
async def clear_customecho(request: Request, login: str, name: str, caller: Caller = WRITE) -> dict[str, Any]:
    settings = _channel(request, login)
    name = name.lower().removeprefix(settings.prefix)
    ctx, key, current = await _customecho(request, settings, caller)
    if not isinstance(current, dict) or name not in current:
        raise HTTPException(status_code=404, detail=f"{name} has no custom wording")
    await commit(request, ctx, WriteOp("delete", key, path=(name,)))
    return {"command": name, "removed": True}


class ValueBody(BaseModel):
    value: Any


@router.put("/channels/{login}/variables/{name}")
async def set_channel_variable(
    request: Request, login: str, name: str, body: ValueBody, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    """`!var set channel.<name>`: for whoever reaches the channel's `channel_var_write_role`."""
    settings = _channel(request, login)
    check_setting_role(request, caller, settings.login, "channel_var_write_role", "write channel variables")
    if body.value is None:
        raise HTTPException(status_code=400, detail="a value can't be null; delete the variable instead")
    ctx = web_context(request, settings, caller)
    await commit(request, ctx, WriteOp("set", _var_key(ctx, "channel", name), body.value))
    return {"name": name, "value": body.value}


@router.delete("/channels/{login}/variables/{name}")
async def delete_channel_variable(
    request: Request, login: str, name: str, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    check_setting_role(request, caller, settings.login, "channel_var_write_role", "write channel variables")
    ctx = web_context(request, settings, caller)
    key = _var_key(ctx, "channel", name)
    if await _state(request, "variable_store").get(key) is MISSING:
        raise HTTPException(status_code=404, detail=f"channel.{name} isn't set")
    await commit(request, ctx, WriteOp("delete", key))
    return {"name": name, "removed": True}


# ── capabilities ────────────────────────────────────────────────────────────
@router.post("/channels/{login}/capabilities/probe")
async def probe_capabilities(request: Request, login: str, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    """Measure again what the bot may do in the channel (a moderator or not), and store it."""
    settings = _channel(request, login)
    found = await _state(request, "capabilities").probe(settings.channel_id)
    return {"login": settings.login, "capabilities": sorted(found)}
