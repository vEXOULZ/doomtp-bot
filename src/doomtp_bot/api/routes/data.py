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

A moderator's session reaches the `READ`/`WRITE` routes of its own channels only, and `BROADCASTER_*`
ones only in their own channel (or with a custom role as high); `ADMIN_*` routes are for admins
(`api/access.py`, ADR-0017, ADR-0026). A channel's chat log is public while its `public_log` setting is on.
Writes are audited as the caller: `via="api"`, or `via="web"` with the user's id for a signed-in user.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from doomtp_bot.api.access import (
    ADMIN_READ,
    ADMIN_WRITE,
    API_ACTOR,
    BROADCASTER_WRITE,
    PERSONAL_READ,
    PERSONAL_WRITE,
    RANK_NAMES,
    READ,
    SETTING_RANKS,
    WRITE,
    WRITE_OWN,
    Caller,
    authenticate,
    check_area,
    check_setting_role,
    current_session,
)
from doomtp_bot.api.routes.session import client_address
from doomtp_bot.audit.log import read_audit
from doomtp_bot.chatlog import queries, timeline
from doomtp_bot.clock import now_ms
from doomtp_bot.core.channels import ChannelBanned
from doomtp_bot.customcmds.packs import custom_modules
from doomtp_bot.customcmds.params import to_params
from doomtp_bot.customcmds.resolution import system_specs
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.filters.matcher import FilterError
from doomtp_bot.filters.service import FilterService
from doomtp_bot.history.queue import BackfillQueue, BackfillRefused
from doomtp_bot.policy.roles import BOT_ADMIN_RANK, GLOBAL
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.policy.snapshot import ChannelSettings
from doomtp_bot.runtime.spec import CommandSpec, LogLevel
from doomtp_bot.runtime.variables import (
    LIMIT_COLUMNS,
    MAX_LIST_ITEMS,
    MAX_NAMES_PER_SPACE,
    MAX_QUOTA_BYTES,
    MAX_VALUE_BYTES,
    OWNER_KINDS,
    Limits,
    Space,
)
from doomtp_bot.triggers.service import TRIGGER_TYPES, TriggerError, TriggerService
from doomtp_bot.variables.store import LimitOverride
from doomtp_bot.webfetch.fetcher import Secret
from doomtp_bot.webfetch.hosts import MAX_LIMIT, HostError, HostStore

router = APIRouter(prefix="/api/v1", tags=["data"])

MAX_ROWS = 500
SETTABLE = (
    "prefix", "quiet_errors", "cc_edit_notice", "log_enabled", "history_backfill", "public_log", "reply_hold_ms",
    "timezone", "automod_action", "automod_timeout_s", "channel_var_write_role", "grant_min_role",
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
        "public_log": settings.public_log,
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
    public_log: bool | None = None
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
async def part_channel(request: Request, login: str, caller: Caller = BROADCASTER_WRITE) -> dict[str, Any]:
    """Leave the channel, as `!part` does: the broadcaster's call, or an admin's."""
    settings = _channel(request, login)
    await _state(request, "channels").part(settings.channel_id, caller.actor)
    return {"login": settings.login, "status": "parted"}


@router.post("/me/channel", status_code=201)
async def join_own_channel(request: Request, caller: Caller = PERSONAL_WRITE) -> dict[str, Any]:
    """Add the bot to the signed-in user's own channel, the way `!join` does (ADR-0026). Signing in with
    Twitch proved the channel is theirs. The session manages it from now on, without waiting for a refresh."""
    if caller.user_id is None or caller.login is None:
        raise HTTPException(status_code=400, detail="sign in with Twitch to add the bot to your channel")
    login = caller.login.lower()
    try:
        failed = await _state(request, "channels").join(caller.user_id, login, caller.actor)
    except ChannelBanned as exc:
        raise HTTPException(status_code=409, detail=f"{exc}; only an admin can rejoin it") from exc
    session = await current_session(request)
    if session is not None and session.channels is not None:
        session.channels = session.channels | {login}
        if session.role == "user":
            session.role = "moderator"
    return {"login": login, "channel_id": caller.user_id, "failed_subscriptions": failed}


@router.patch("/channels/{login}")
async def patch_channel(
    request: Request, login: str, body: ChannelPatch, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    policy = _policy(request)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in SETTABLE}
    if not changes:
        raise HTTPException(status_code=400, detail=f"nothing to change; fields: {', '.join(SETTABLE)}")
    # Each field needs the rank chat asks for it; one field too high refuses the whole patch (ADR-0026).
    rank = caller.rank_in(policy, settings.login)
    refused = sorted(k for k in changes if SETTING_RANKS[k] > rank)
    if refused:
        who = RANK_NAMES.get(max(SETTING_RANKS[k] for k in refused), "an admin")
        raise HTTPException(status_code=403, detail=f"only {who} can change {', '.join(refused)}")

    async def write(repo: Any) -> None:
        for column, value in changes.items():
            await repo.set_channel_field(settings.channel_id, column, value, caller.actor)

    await policy.mutate(write)  # one snapshot reload for the whole patch
    return _channel_json(_channel(request, settings.login))


# ── backfill jobs (ADR-0024 §5) ─────────────────────────────────────────────


class BackfillRequest(BaseModel):
    """A range (`to_ms` defaults to now), or `gaps` for every open gap in the channel's log."""

    from_ms: int | None = Field(default=None, ge=0)
    to_ms: int | None = Field(default=None, ge=0)
    gaps: bool = False


def _backfill(request: Request) -> BackfillQueue:
    return _state(request, "backfill")  # type: ignore[no-any-return]


@router.get("/channels/{login}/backfill")
async def list_backfill_jobs(
    request: Request, login: str, limit: int = Query(20, ge=1, le=100), caller: Caller = READ
) -> dict[str, Any]:
    """Queued and running jobs first, oldest first; then the latest finished ones."""
    settings = _channel(request, login)
    jobs = await _backfill(request).jobs(settings.channel_id, limit=limit)
    return {"enabled": settings.history_backfill, "jobs": [j.to_json() for j in jobs]}


@router.post("/channels/{login}/backfill", status_code=201)
async def queue_backfill(
    request: Request, login: str, body: BackfillRequest, caller: Caller = BROADCASTER_WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    queue = _backfill(request)
    if body.gaps == (body.from_ms is not None):
        raise HTTPException(status_code=400, detail="send from_ms (and to_ms), or gaps: true")
    try:
        if body.gaps:
            # The channel's one job for its gaps; one already waiting is answered rather than refused.
            job, _ = await queue.queue_gaps(settings.channel_id, caller.label)
            jobs = [] if job is None else [job]
        else:
            assert body.from_ms is not None
            to_ms = now_ms() if body.to_ms is None else body.to_ms
            job = await queue.queue_range(settings.channel_id, body.from_ms, to_ms, caller.label)
            if job is None:
                raise HTTPException(status_code=409, detail="that range is already queued")
            jobs = [job]
    except BackfillRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"jobs": [j.to_json() for j in jobs]}


@router.delete("/channels/{login}/backfill/{job_id}")
async def cancel_backfill(
    request: Request, login: str, job_id: int, caller: Caller = BROADCASTER_WRITE
) -> dict[str, Any]:
    """Only a queued job can be cancelled: a running one is already asking the provider."""
    settings = _channel(request, login)
    job = await _backfill(request).cancel(settings.channel_id, job_id)
    if job is None:
        raise HTTPException(status_code=409, detail=f"job {job_id} isn't queued in {settings.login}")
    return job.to_json()


async def _module_specs(request: Request, channel_id: str) -> dict[str, tuple[CommandSpec, str]]:
    """Every module `!module` knows in a channel: the built-in ones, then the packs published there or
    globally and `custom` (ADR-0012). For `GLOBAL`, the packs published globally."""
    specs: dict[str, tuple[CommandSpec, str]] = {
        c.spec.module: (c.spec, "builtin") for c in _state(request, "runtime").registry.all()
    }
    services = request.app.state
    found = await custom_modules(
        channel_id, getattr(services, "packs", None), getattr(services, "customcmds", None)
    )
    for name, kind in found.items():
        specs.setdefault(name, (CommandSpec(name="", module=name, summary=""), kind))
    return specs


async def toggle_module(
    request: Request, scope: str, module: str, enabled: bool | None, caller: Caller
) -> dict[str, Any]:
    """`!module enable|disable|reset` in `scope` (a channel, or `GLOBAL`); `None` goes back to inheriting."""
    specs = await _module_specs(request, scope)
    if module not in specs:
        raise HTTPException(status_code=404, detail=f"no module named {module}")
    if not specs[module][0].toggleable:
        raise HTTPException(status_code=400, detail=f"{module} can't be turned off")
    await _policy(request).mutate(lambda repo: repo.set_module_toggle(scope, module, enabled, caller.actor))
    return {"module": module, "enabled": enabled}


@router.get("/channels/{login}/modules")
async def list_modules(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    """Each module, whether it is on here, and whether it can be turned off at all.

    `kind` says where it comes from: `builtin`, a `pack` published here or globally (its name is its
    module name, ADR-0012), or `custom`, which holds the commands published one by one and appears when
    there are any. `enabled` follows the rules chat applies: this channel's toggle, then the global one,
    then on. Turn any of them off with `PUT …/modules/{module}`, as `!module disable` does in chat.
    """
    settings, policy = _channel(request, login), _policy(request)
    specs = await _module_specs(request, settings.channel_id)
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


def _check_bot_wide(caller: Caller) -> None:
    """A change to the `GLOBAL` scope reaches every channel, so it is for admins (ADR-0017). This is the
    only write here that can reach `GLOBAL`: filters, toggles, rules and triggers are always written to
    the channel in the path, and a moderator can't reach a global entry through it."""
    if not caller.is_admin:
        raise HTTPException(status_code=403, detail="only an admin can change a bot-wide ignore")


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
    """Ignore a user here, or everywhere with `everywhere: true` (admins only). Audited as `ignore.add`,
    like chat."""
    if body.everywhere:
        _check_bot_wide(caller)
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

    `everywhere` is for admins. A signed-in user may also lift an ignore they set on themselves (`ignore
    me`), in any channel; that one is always per channel, so `everywhere` never applies to it."""
    if everywhere:
        _check_bot_wide(caller)
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
    return await toggle_module(request, settings.channel_id, module, body.enabled, caller)


@router.delete("/channels/{login}/modules/{module}")
async def reset_module(request: Request, login: str, module: str, caller: Caller = WRITE) -> dict[str, Any]:
    """Back to what the bot-wide toggle says, as `!module reset` does."""
    settings = _channel(request, login)
    return await toggle_module(request, settings.channel_id, module, None, caller)


# ── commands ────────────────────────────────────────────────────────────────
LogLevelName = Literal["off", "errors", "output", "invocations", "all"]


class CooldownBody(BaseModel):
    tier_s: int = Field(ge=0, le=86_400)  # shared by everyone at that role
    user_s: int = Field(ge=0, le=86_400)  # per user


class CommandRule(BaseModel):
    """Only the fields sent change. `enabled: null` goes back to the module's setting; `required_role:
    null` clears the rule here (and its `allowed_roles`); a role's cooldown set to `null` is cleared."""

    enabled: bool | None = None
    required_role: str | None = None
    allowed_roles: list[str] | None = Field(default=None, max_length=20)
    cooldowns: dict[str, CooldownBody | None] | None = None
    log_level: LogLevelName | None = None


def command_spec(request: Request, name: str) -> CommandSpec:
    """A command as `!perm` and `!cmd` find it: in the registry, then among the system packs' members."""
    runtime = _state(request, "runtime")
    wanted = name.lower()
    found = runtime.registry.get(wanted)
    if found is not None:
        return found.spec  # type: ignore[no-any-return]
    for spec in system_specs(runtime.resolver):
        if spec.name == wanted:
            return spec
    raise HTTPException(status_code=404, detail=f"no command named {name}")


def rule_json(policy: PolicyService, scope: str, spec: CommandSpec) -> dict[str, Any]:
    required, allowed = policy.required_role(scope, spec)
    return {
        "name": spec.name,
        "module": spec.module,
        "summary": spec.summary,
        "enabled": policy.is_enabled(scope, spec),
        "toggleable": spec.toggleable,
        "fixed_policy": spec.fixed_policy,
        "required_role": required,
        "allowed_roles": list(allowed) if allowed else None,
        "cooldowns": {
            role: {"tier_s": cd.tier_s, "user_s": cd.user_s}
            for role, cd in policy.cooldown_rules(scope, spec).items()
        },
        "log_level": policy.log_level(scope, spec).value,
    }


@router.get("/channels/{login}/commands")
async def channel_commands(request: Request, login: str) -> dict[str, Any]:
    """What this channel's chat can actually run, with the rules that apply here. Public, like the page."""
    settings = _channel(request, login)
    policy, runtime = _policy(request), _state(request, "runtime")
    listing = [
        {
            **rule_json(policy, settings.channel_id, spec),
            "requires": list(spec.requires),
            "missing": sorted(set(spec.requires) - set(settings.capabilities)),
        }
        for spec in [c.spec for c in runtime.registry.all()] + system_specs(runtime.resolver)
    ]
    return {"channel": settings.login, "prefix": settings.prefix, "commands": listing}


async def apply_command_rule(
    request: Request, scope: str, spec: CommandSpec, body: CommandRule, caller: Caller, rank: int
) -> None:
    """`!cmd`, `!perm` and `!cooldown` in one change, each checked the way chat checks it (ADR-0026)."""
    policy = _policy(request)
    fields = body.model_dump(exclude_unset=True)
    rules = {"required_role", "allowed_roles", "cooldowns"} & set(fields)
    if rules and spec.fixed_policy:
        raise HTTPException(status_code=400, detail=f"{spec.name} is always allowed for everyone")
    if rules and spec.module == "core_admin" and rank < BOT_ADMIN_RANK:
        raise HTTPException(status_code=403, detail="only a bot admin can change an admin command's rules")
    if "enabled" in fields and not spec.toggleable:
        raise HTTPException(status_code=400, detail=f"{spec.name} can't be turned off")
    if fields.get("allowed_roles") == []:
        raise HTTPException(status_code=400, detail="allowed_roles needs a role, or null to clear it")
    named = [fields.get("required_role"), *(body.allowed_roles or []), *(body.cooldowns or {})]
    unknown = sorted({r for r in named if r is not None and policy.rank_of(scope, r) is None})
    if unknown:
        raise HTTPException(status_code=400, detail=f"no role named {', '.join(unknown)}")

    async def write(repo: Any) -> None:
        if "enabled" in fields or "log_level" in fields:
            await repo.set_command_toggle(
                scope,
                spec.name,
                caller.actor,
                enabled=fields.get("enabled"),
                log_level=fields.get("log_level"),
                clear_enabled="enabled" in fields and fields["enabled"] is None,
            )
        if "required_role" in fields or "allowed_roles" in fields:
            current, allowed = policy.required_role(scope, spec)
            required = fields.get("required_role", current)
            roles = fields.get("allowed_roles", list(allowed) if allowed else None)
            if required is None:
                roles = None
            await repo.set_command_rule(scope, spec.name, required, roles, caller.actor)
        for role, cooldown in (body.cooldowns or {}).items():
            tier, user = (None, None) if cooldown is None else (cooldown.tier_s, cooldown.user_s)
            await repo.set_cooldown(scope, spec.name, role, tier, user, caller.actor)

    await policy.mutate(write)


@router.patch("/channels/{login}/commands/{name}")
async def patch_command(
    request: Request, login: str, name: str, body: CommandRule, caller: Caller = WRITE
) -> dict[str, Any]:
    """Turn a command on or off here, set who may use it, its cooldowns per role and how much of its runs
    is logged: `!cmd`, `!perm` and `!cooldown` in one call."""
    settings, policy = _channel(request, login), _policy(request)
    spec = command_spec(request, name)
    rank = caller.rank_in(policy, settings.login)
    await apply_command_rule(request, settings.channel_id, spec, body, caller, rank)
    return rule_json(policy, settings.channel_id, spec)


def reset_rule(policy: PolicyService, scope: str, spec: CommandSpec) -> CommandRule:
    """What `DELETE` sends: `!cmd reset`, `!perm clear` and every cooldown cleared, where each applies."""
    fields: dict[str, Any] = {"enabled": None} if spec.toggleable else {}
    if not spec.fixed_policy:
        fields["required_role"] = None
        fields["cooldowns"] = dict.fromkeys(policy.cooldown_rules(scope, spec))
    return CommandRule(**fields)


@router.delete("/channels/{login}/commands/{name}")
async def reset_command(request: Request, login: str, name: str, caller: Caller = WRITE) -> dict[str, Any]:
    """Back to the command's own rules here. The log level stays."""
    settings, policy = _channel(request, login), _policy(request)
    spec = command_spec(request, name)
    rank = caller.rank_in(policy, settings.login)
    body = reset_rule(policy, settings.channel_id, spec)
    await apply_command_rule(request, settings.channel_id, spec, body, caller, rank)
    return rule_json(policy, settings.channel_id, spec)


# ── filters ─────────────────────────────────────────────────────────────────
FilterKind = Literal["word", "wildcard", "regex", "allow"]
FilterAction = Literal["mask", "replace", "tag", "block"]


class FilterBody(BaseModel):
    pattern: str = Field(min_length=1, max_length=200)
    kind: FilterKind = "word"
    action: FilterAction = "mask"
    category: str = Field(default="", max_length=40)
    replacement: str = Field(default="", max_length=200)


class FilterPatch(BaseModel):
    """Only the fields sent change."""

    enabled: bool | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=200)
    kind: FilterKind | None = None
    action: FilterAction | None = None
    category: str | None = Field(default=None, max_length=40)
    replacement: str | None = Field(default=None, max_length=200)


def filter_json(e: Any) -> dict[str, Any]:
    return {
        "id": e.id,
        "pattern": e.pattern,
        "kind": e.kind,
        "action": e.action,
        "category": e.category,
        "replacement": e.replacement,
        "enabled": e.enabled,
        "global": e.channel_id == GLOBAL,
    }


def _where(scope: str) -> str:
    return "everywhere" if scope == GLOBAL else "here"


@router.get("/channels/{login}/filters")
async def list_filters(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    settings = _channel(request, login)
    filters: FilterService = _state(request, "filters")
    return {"filters": [filter_json(e) for e in filters.entries_for(settings.channel_id)]}


@router.post("/channels/{login}/filters", status_code=201)
async def add_filter(
    request: Request, login: str, body: FilterBody, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    return await add_filter_to(request, settings.channel_id, body, caller)


async def add_filter_to(request: Request, scope: str, body: FilterBody, caller: Caller) -> dict[str, Any]:
    filters: FilterService = _state(request, "filters")
    try:
        entry = await filters.add(
            channel_id=scope,
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
    return filter_json(entry)


@router.patch("/channels/{login}/filters/{entry_id}")
async def patch_filter(
    request: Request, login: str, entry_id: int, body: FilterPatch, caller: Caller = WRITE
) -> dict[str, Any]:
    """Turn an entry on or off, or change it, checked as a new one is. Audited as `filter.edit`."""
    settings = _channel(request, login)
    return await patch_filter_in(request, settings.channel_id, entry_id, body, caller)


async def patch_filter_in(
    request: Request, scope: str, entry_id: int, body: FilterPatch, caller: Caller
) -> dict[str, Any]:
    filters: FilterService = _state(request, "filters")
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    enabled = fields.pop("enabled", None)
    if not fields and enabled is None:
        raise HTTPException(status_code=400, detail="nothing to change")
    missing = HTTPException(status_code=404, detail=f"no filter {entry_id} {_where(scope)}")
    if fields:
        try:
            entry = await filters.update(
                channel_id=scope,
                entry_id=entry_id,
                fields=fields,
                actor_user_id=caller.actor.user_id,
                via=caller.actor.via,
            )
        except FilterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if entry is None:
            raise missing
    if enabled is not None and not await filters.set_enabled(
        channel_id=scope,
        entry_id=entry_id,
        enabled=enabled,
        actor_user_id=caller.actor.user_id,
        via=caller.actor.via,
    ):
        raise missing
    found = next(e for e in filters.entries_for(scope) if e.id == entry_id)
    return filter_json(found)


@router.delete("/channels/{login}/filters/{entry_id}")
async def remove_filter(
    request: Request, login: str, entry_id: int, caller: Caller = WRITE
) -> dict[str, Any]:
    settings = _channel(request, login)
    return await remove_filter_from(request, settings.channel_id, entry_id, caller)


async def remove_filter_from(request: Request, scope: str, entry_id: int, caller: Caller) -> dict[str, Any]:
    filters: FilterService = _state(request, "filters")
    if not await filters.remove(
        channel_id=scope,
        entry_id=entry_id,
        actor_user_id=caller.actor.user_id,
        via=caller.actor.via,
    ):
        raise HTTPException(status_code=404, detail=f"no filter {entry_id} {_where(scope)}")
    return {"id": entry_id, "removed": True}


# ── triggers ────────────────────────────────────────────────────────────────
class TriggerBody(BaseModel):
    type: str
    expr: str = Field(min_length=1)
    match: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    run_as_rank: int | None = Field(default=None, ge=0, le=10_000)  # the creator's own rank by default
    log_level: LogLevelName = "output"


class TriggerPatch(BaseModel):
    """Only the fields sent change; the type can't."""

    enabled: bool | None = None
    expr: str | None = Field(default=None, min_length=1)
    match: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    run_as_rank: int | None = Field(default=None, ge=0, le=10_000)
    log_level: LogLevelName | None = None


def _trigger_json(trigger: Any) -> dict[str, Any]:
    return {
        "id": trigger.id,
        "type": trigger.type,
        "expr": trigger.expr,
        "match": trigger.match,
        "schedule": trigger.schedule,
        "enabled": trigger.enabled,
        "run_as_rank": trigger.run_as_rank,
        "log_level": trigger.log_level.value,
        "created_by": trigger.created_by,
    }


def _run_as(request: Request, caller: Caller, login: str, wanted: int | None) -> int:
    """A trigger runs at its creator's rank at most, as `!trigger add` sets it (ADR-0026)."""
    rank = caller.rank_in(_policy(request), login)
    if wanted is None:
        return rank
    if wanted > rank:
        raise HTTPException(status_code=403, detail=f"a trigger can't run above your own rank ({rank})")
    return wanted


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
            run_as_rank=_run_as(request, caller, settings.login, body.run_as_rank),
            log_level=LogLevel(body.log_level),
            created_by=caller.actor.user_id,
            prefix=settings.prefix,
            via=caller.actor.via,
        )
    except TriggerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _trigger_json(trigger)


@router.patch("/channels/{login}/triggers/{trigger_id}")
async def patch_trigger(
    request: Request, login: str, trigger_id: int, body: TriggerPatch, caller: Caller = WRITE
) -> dict[str, Any]:
    """Turn a trigger on or off, or change it, checked as a new one is. Audited as `trigger.edit`."""
    settings = _channel(request, login)
    triggers: TriggerService = _state(request, "triggers")
    fields = {k for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to change")
    missing = HTTPException(status_code=404, detail=f"no trigger {trigger_id} here")
    if fields - {"enabled"}:
        run_as = (
            None if body.run_as_rank is None else _run_as(request, caller, settings.login, body.run_as_rank)
        )
        try:
            updated = await triggers.update(
                channel_id=settings.channel_id,
                trigger_id=trigger_id,
                expr=body.expr,
                match=body.match,
                schedule=body.schedule,
                run_as_rank=run_as,
                log_level=None if body.log_level is None else LogLevel(body.log_level),
                actor_user_id=caller.actor.user_id,
                prefix=settings.prefix,
                via=caller.actor.via,
            )
        except TriggerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise missing
    if body.enabled is not None and not await triggers.set_enabled(
        channel_id=settings.channel_id,
        trigger_id=trigger_id,
        enabled=body.enabled,
        actor_user_id=caller.actor.user_id,
        via=caller.actor.via,
    ):
        raise missing
    return _trigger_json(next(t for t in triggers.in_channel(settings.channel_id) if t.id == trigger_id))


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
                # The version this channel last ran; differs from "version" when the author edited it since.
                "last_run_version": publication.last_run_version,
            }
            for publication, command in found
        ],
    }


@router.patch("/channels/{login}/publications/{name}")
async def set_publication(
    request: Request, login: str, name: str, body: Enabled, caller: Caller = PERSONAL_WRITE
) -> dict[str, Any]:
    """`cc enable|disable`: for whoever reaches the channel's `publish_min_role`, as in chat."""
    settings = _channel(request, login)
    check_setting_role(
        request, caller, settings.login, "publish_min_role", "turn published commands on or off"
    )
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
    """The channel's own variables, with the login of whoever last set each when it's known."""
    settings = _channel(request, login)
    store = _state(request, "variable_store")
    entries = await store.entries(Space("channel", settings.channel_id, "", ""))
    known = {c.channel_id: c.login for c in _policy(request).channels()}
    return {
        "variables": [
            {
                "name": e.key.name,
                "value": e.value,
                "updated_at": e.updated_at,
                "updated_by": e.updated_by,
                "updated_by_login": await _login_of(request, e.updated_by, known),
            }
            for e in entries
        ]
    }


@router.get("/channels/{login}/storage")
async def channel_storage(request: Request, login: str, caller: Caller = READ) -> dict[str, Any]:
    """How much of its quota the channel uses, per namespace (ADR-0019), like `!var usage channel`."""
    settings = _channel(request, login)
    store = _state(request, "variable_store")
    used = await store.usage("channel", settings.channel_id)
    limits = await store.limits_for("channel", settings.channel_id)
    return {
        "used_bytes": sum(used.values()),
        "namespaces": used,
        **_limits_json(limits),
    }


# ── storage limits (ADR-0019): the same writes as `!admin quota|valuecap|listitems|names` ────
class LimitsBody(BaseModel):
    """Only the fields sent change. On an owner, `null` goes back to the default."""

    quota_bytes: int | None = Field(default=None, ge=0, le=MAX_QUOTA_BYTES)
    value_cap_bytes: int | None = Field(default=None, ge=0, le=MAX_VALUE_BYTES)
    list_items: int | None = Field(default=None, ge=0, le=MAX_LIST_ITEMS)
    names_per_space: int | None = Field(default=None, ge=0, le=MAX_NAMES_PER_SPACE)


def _limits_json(limits: Limits | LimitOverride) -> dict[str, int | None]:
    return {column: getattr(limits, column) for column in LIMIT_COLUMNS}


async def _apply_limits(request: Request, kind: str, owner_id: str, body: LimitsBody, caller: Caller) -> None:
    store = _state(request, "variable_store")
    for field in sorted(body.model_fields_set):
        value = getattr(body, field)
        if kind == "*" and value is None:
            raise HTTPException(status_code=422, detail=f"the default {field} can't be null")
        await store.set_limit(kind, owner_id, field, value, actor=caller.actor.user_id, via=caller.actor.via)


@router.get("/variable-limits")
async def variable_limits(request: Request, caller: Caller = ADMIN_READ) -> dict[str, Any]:
    store = _state(request, "variable_store")
    defaults = await store.defaults()
    return {
        "defaults": _limits_json(defaults),
        "overrides": [
            {"owner_kind": kind, "owner_id": owner_id, **_limits_json(o)}
            for kind, owner_id, o in await store.overrides()
        ],
    }


@router.patch("/variable-limits/default")
async def set_default_limits(
    request: Request, body: LimitsBody, caller: Caller = ADMIN_WRITE
) -> dict[str, Any]:
    await _apply_limits(request, "*", "*", body, caller)
    defaults = await _state(request, "variable_store").defaults()
    return _limits_json(defaults)


@router.patch("/variable-limits/{kind}/{user}")
async def set_owner_limits(
    request: Request, kind: str, user: str, body: LimitsBody, caller: Caller = ADMIN_WRITE
) -> dict[str, Any]:
    """One channel's, publisher's or chatter's override, by Twitch login."""
    if kind not in OWNER_KINDS:
        raise HTTPException(status_code=404, detail=f"owner kind must be one of {', '.join(OWNER_KINDS)}")
    found = await _state(request, "twitch").resolve_user(user)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no Twitch user named {user}")
    await _apply_limits(request, kind, found["id"], body, caller)
    store = _state(request, "variable_store")
    own = await store.override(kind, found["id"])
    limits = await store.limits_for(kind, found["id"])
    return {
        "owner_kind": kind,
        "owner_id": found["id"],
        "override": None if own is None else _limits_json(own),
        "effective": _limits_json(limits),
    }


# ── the hosts `http get` may fetch (ADR-0020): the same writes as `!admin http` ──────────────
# A secret goes in here and never comes back out: listings show its kind and name only.
class HostBody(BaseModel):
    plain_http: bool = False


class SecretBody(BaseModel):
    kind: Literal["query", "header"]
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=512)


class HttpLimitsBody(BaseModel):
    channel_per_minute: int | None = Field(default=None, ge=0, le=MAX_LIMIT)
    host_per_minute: int | None = Field(default=None, ge=0, le=MAX_LIMIT)


def _hosts(request: Request) -> HostStore:
    hosts: HostStore = _state(request, "http_hosts")
    return hosts


@router.get("/http-hosts")
async def http_hosts(request: Request, caller: Caller = ADMIN_READ) -> dict[str, Any]:
    hosts = _hosts(request)
    return {"hosts": [e.public() for e in hosts.entries()], "limits": hosts.limits().as_dict()}


@router.put("/http-hosts/{pattern}")
async def allow_http_host(
    request: Request, pattern: str, body: HostBody, caller: Caller = ADMIN_WRITE
) -> dict[str, Any]:
    try:
        entry = await _hosts(request).allow(
            pattern, plain_http=body.plain_http, actor=caller.actor.user_id, via=caller.actor.via
        )
    except HostError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return entry.public()


@router.delete("/http-hosts/{pattern}")
async def deny_http_host(request: Request, pattern: str, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    try:
        removed = await _hosts(request).deny(pattern, actor=caller.actor.user_id, via=caller.actor.via)
    except HostError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not removed:
        raise HTTPException(status_code=404, detail=f"{pattern} isn't on the list")
    return {"pattern": pattern.lower(), "status": "removed"}


@router.put("/http-hosts/{pattern}/secret")
async def set_http_secret(
    request: Request, pattern: str, body: SecretBody, caller: Caller = ADMIN_WRITE
) -> dict[str, Any]:
    secret = Secret(body.kind, body.name, body.value)
    try:
        entry = await _hosts(request).set_secret(
            pattern, secret, actor=caller.actor.user_id, via=caller.actor.via
        )
    except HostError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return entry.public()


@router.delete("/http-hosts/{pattern}/secret")
async def clear_http_secret(request: Request, pattern: str, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    try:
        entry = await _hosts(request).set_secret(
            pattern, None, actor=caller.actor.user_id, via=caller.actor.via
        )
    except HostError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return entry.public()


@router.patch("/http-limits")
async def set_http_limits(
    request: Request, body: HttpLimitsBody, caller: Caller = ADMIN_WRITE
) -> dict[str, int]:
    limits = await _hosts(request).set_limits(
        channel_per_minute=body.channel_per_minute,
        host_per_minute=body.host_per_minute,
        actor=caller.actor.user_id,
        via=caller.actor.via,
    )
    return limits.as_dict()


# ── the chat log: moderators, or anyone while it is public (ADR-0026) ──────
PUBLIC_READER = Caller("public", API_ACTOR, role="user", channels=frozenset())


def _is_public(caller: Caller) -> bool:
    return caller is PUBLIC_READER


async def _log_reader(request: Request) -> Caller:
    """A moderator of the channel, as for `READ`; failing that, anyone at all while the channel's log is
    public (`public_log` and `log_enabled`), a limited number of reads a minute per address."""
    login = request.path_params["login"]
    try:
        caller = await authenticate(request, "read")
        check_area(caller, "channel", login)
        return caller
    except HTTPException:
        settings = _policy(request).channel_by_login(login)
        if settings is None or not (settings.public_log and settings.log_enabled):
            raise
    wait = request.app.state.public_read_limiter.hit(client_address(request))
    if wait is not None:
        raise HTTPException(
            status_code=429, detail="too many reads; try again later", headers={"Retry-After": str(wait)}
        )
    return PUBLIC_READER


# Read by the test that walks every route.
_log_reader.area = "channel"  # type: ignore[attr-defined]
_log_reader.min_rank = None  # type: ignore[attr-defined]
_log_reader.public = True  # type: ignore[attr-defined]
LOG_READ = Depends(_log_reader)


@router.get("/channels/{login}/runs")
async def command_runs(
    request: Request,
    login: str,
    limit: int = Query(default=50, ge=1, le=MAX_ROWS),
    caller: Caller = READ,
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
    caller: Caller = LOG_READ,
) -> dict[str, Any]:
    """Full-text search over the channel's log (architecture §3.3). The public see no removed messages."""
    settings = _channel(request, login)
    rows = await queries.search_messages(
        _state(request, "chatlog_db"), settings.channel_id, q, limit=limit, visible_only=_is_public(caller)
    )
    return {"query": q, "messages": rows}


# A list default must be a module-level singleton (ruff B008); repeat `kind=` for several, all by default.
_KINDS_QUERY = Query(default=None, description="message, notification or moderation; repeat for several")


@router.get("/channels/{login}/log")
async def channel_log(
    request: Request,
    login: str,
    since: int | None = Query(default=None, ge=0, description="ms since the epoch, inclusive"),
    until: int | None = Query(default=None, ge=0, description="ms since the epoch, exclusive"),
    order: timeline.Order = "desc",
    kind: list[timeline.Kind] | None = _KINDS_QUERY,
    user: str | None = Query(default=None, min_length=1, max_length=40, description="a login, old ones too"),
    q: str | None = Query(default=None, min_length=1, max_length=queries.MAX_QUERY_CHARS),
    hide_removed: bool = False,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=100, ge=1, le=MAX_ROWS),
    caller: Caller = LOG_READ,
) -> dict[str, Any]:
    """The channel's log as one timeline of messages, notifications and moderation, a page at a time
    (ADR-0025). Pass `next` back as `cursor`, with the same filters, for the page after. The public get
    messages and notifications only, without removed messages (ADR-0026)."""
    settings = _channel(request, login)
    kinds = list(kind or timeline.KINDS)
    if _is_public(caller):
        kinds, hide_removed = [k for k in kinds if k != "moderation"], True
        if not kinds:
            raise HTTPException(status_code=403, detail=f"only a moderator can read {login}'s moderation log")
    conn = _state(request, "chatlog_db")
    user_ids = None if user is None else await timeline.user_ids_for(conn, user)
    try:
        after = None if cursor is None else timeline.Cursor.decode(cursor)
        entries, following = await timeline.read(
            conn, settings.channel_id, kinds=kinds, since=since, until=until, cursor=after,
            order=order, limit=limit, user_ids=user_ids, query=q, hide_removed=hide_removed,
        )  # fmt: skip
    except timeline.CursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "channel_id": settings.channel_id,
        "order": order,
        "entries": entries,
        "next": None if following is None else following.encode(),
    }


@router.get("/channels/{login}/log/coverage")
async def channel_log_coverage(
    request: Request,
    login: str,
    since: int = Query(ge=0, description="ms since the epoch"),
    until: int | None = Query(default=None, ge=0, description="ms since the epoch; now by default"),
    caller: Caller = LOG_READ,
) -> dict[str, Any]:
    """When the bot was listening between `since` and `until`, and which holes backfill filled (ADR-0025)."""
    if until is not None and until <= since:
        raise HTTPException(status_code=422, detail="until must be after since")
    settings = _channel(request, login)
    return await timeline.coverage(_state(request, "chatlog_db"), settings.channel_id, since, until)


@router.get("/audit")
async def audit(
    request: Request,
    channel: str | None = Query(default=None, max_length=40),
    actor: str | None = Query(default=None, max_length=40, description="a login, or `me`"),
    action: str | None = Query(
        default=None, max_length=64, description="`cc.edit`, or `cc.` for every cc one"
    ),
    before: int | None = Query(default=None, ge=1, description="the `next` of the page before"),
    limit: int = Query(default=50, ge=1, le=MAX_ROWS),
    caller: Caller = PERSONAL_READ,
) -> dict[str, Any]:
    """The newest changes first, a page at a time: pass `next` back as `before`. A moderator sees the
    channels they manage; anyone signed in sees their own changes everywhere with `actor=me` (ADR-0026)."""
    conn, policy = _state(request, "bot_db"), _policy(request)
    known = {c.channel_id: c.login for c in policy.channels()}
    own = actor == "me"
    actor_id: str | None = None
    if own:
        if caller.user_id is None:
            raise HTTPException(status_code=400, detail="actor=me needs a Twitch sign-in")
        actor_id = caller.user_id
    elif actor is not None:
        user = await _state(request, "twitch").resolve_user(actor)
        if user is None:
            raise HTTPException(status_code=404, detail=f"no Twitch user named {actor}")
        actor_id = user["id"]
    scope: dict[str, Any] = {}
    if channel is not None:
        if not own:
            check_area(caller, "channel", channel)
        scope["channel_id"] = _channel(request, channel).channel_id
    elif not caller.is_admin and not own:
        if caller.role == "user":
            # Someone who manages no channel sees only their own changes.
            if caller.user_id is None or actor_id not in (None, caller.user_id):
                return {"entries": [], "next": None}
            actor_id = caller.user_id
        else:
            scope["channel_ids"] = [c.channel_id for c in policy.channels() if caller.manages(c.login)]
    found = await read_audit(
        conn, limit=limit, before_id=before, actor_user_id=actor_id, action=action, **scope
    )
    for entry in found:
        entry["channel_login"] = known.get(entry.get("channel_id") or "")
        entry["actor_login"] = await _login_of(request, entry.get("actor_user_id"), known)
    return {"entries": found, "next": found[-1]["id"] if len(found) == limit else None}
