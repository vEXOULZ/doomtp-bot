"""`/api/v1/admins`, `/api/v1/global/…`: what reaches every channel, for bot admins (ADR-0017, ADR-0026).

The bot admins themselves (`!admin add|remove`, for bot owners), the bot-wide module and command
toggles and rules, the bot-wide word filter, who is ignored everywhere, and the commands and packs
published everywhere (ADR-0012). The channel routes' helpers do the work with `GLOBAL` as the scope, so
the checks and the audit entries are the ones a channel change gets.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from doomtp_bot.api.access import ADMIN_READ, ADMIN_WRITE, Caller
from doomtp_bot.api.routes.commands import (
    PackRef,
    PublishBody,
    publish_in,
    publish_pack_in,
    unpublish_in,
    unpublish_pack_in,
)
from doomtp_bot.api.routes.data import (
    CommandRule,
    Enabled,
    FilterBody,
    FilterPatch,
    _ignored_json,
    _login_of,
    _module_specs,
    _policy,
    _state,
    add_filter_to,
    apply_command_rule,
    command_spec,
    filter_json,
    patch_filter_in,
    remove_filter_from,
    reset_rule,
    rule_json,
    toggle_module,
)
from doomtp_bot.customcmds.resolution import system_specs
from doomtp_bot.policy.roles import BOT_OWNER_RANK, GLOBAL

router = APIRouter(prefix="/api/v1", tags=["bot"])


# ── bot admins (`!admin add|remove`) ────────────────────────────────────────
@router.get("/admins")
async def list_admins(request: Request, caller: Caller = ADMIN_READ) -> dict[str, Any]:
    """The owners (set in the bot's configuration) and the admins they added."""
    policy = _policy(request)
    known = {c.channel_id: c.login for c in policy.channels()}
    owners = sorted(policy.owners)
    admins = sorted(policy.global_admin_ids() - set(owners))
    return {
        "owners": [{"user_id": u, "login": await _login_of(request, u, known)} for u in owners],
        "admins": [{"user_id": u, "login": await _login_of(request, u, known)} for u in admins],
        "you_manage": _is_owner(request, caller),
    }


def _is_owner(request: Request, caller: Caller) -> bool:
    return caller.rank_in(_policy(request), "") >= BOT_OWNER_RANK


def _check_owner(request: Request, caller: Caller) -> None:
    """`!admin` is for bot owners in chat, so here too."""
    if not _is_owner(request, caller):
        raise HTTPException(status_code=403, detail="only a bot owner can add or remove bot admins")


class AdminBody(BaseModel):
    login: str = Field(min_length=1, max_length=40)


@router.post("/admins", status_code=201)
async def add_admin(request: Request, body: AdminBody, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    _check_owner(request, caller)
    user = await _state(request, "twitch").resolve_user(body.login.lstrip("@"))
    if user is None:
        raise HTTPException(status_code=404, detail=f"no Twitch user named {body.login}")
    await _policy(request).mutate(lambda repo: repo.set_global_admin(user["id"], user["name"], True, caller.actor))
    return {"user_id": user["id"], "login": user["name"], "admin": True}


@router.delete("/admins/{user_id}")
async def remove_admin(request: Request, user_id: str, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    _check_owner(request, caller)
    policy = _policy(request)
    if user_id in policy.owners:
        raise HTTPException(status_code=400, detail="a bot owner is set in the configuration")
    if user_id not in policy.global_admin_ids():
        raise HTTPException(status_code=404, detail=f"{user_id} isn't a bot admin")
    login = await _login_of(request, user_id, {}) or ""
    await policy.mutate(lambda repo: repo.set_global_admin(user_id, login, False, caller.actor))
    return {"user_id": user_id, "admin": False}


# ── bot-wide modules and commands ───────────────────────────────────────────
@router.get("/global/modules")
async def global_modules(request: Request, caller: Caller = ADMIN_READ) -> dict[str, Any]:
    """Each module's bot-wide toggle: `null` leaves it to each channel (on unless a channel turns it off)."""
    policy = _policy(request)
    specs = await _module_specs(request, GLOBAL)
    return {
        "modules": [
            {
                "module": m,
                "enabled": policy.is_enabled(GLOBAL, s),
                "toggleable": s.toggleable,
                "kind": kind,
            }
            for m, (s, kind) in sorted(specs.items())
        ]
    }


@router.put("/global/modules/{module}")
async def set_global_module(
    request: Request, module: str, body: Enabled, caller: Caller = ADMIN_WRITE
) -> dict[str, Any]:
    return await toggle_module(request, GLOBAL, module, body.enabled, caller)


@router.delete("/global/modules/{module}")
async def reset_global_module(request: Request, module: str, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    return await toggle_module(request, GLOBAL, module, None, caller)


@router.get("/global/commands")
async def global_commands(request: Request, caller: Caller = ADMIN_READ) -> dict[str, Any]:
    policy, runtime = _policy(request), _state(request, "runtime")
    specs = [c.spec for c in runtime.registry.all()] + system_specs(runtime.resolver)
    return {"commands": [rule_json(policy, GLOBAL, spec) for spec in specs]}


@router.patch("/global/commands/{name}")
async def patch_global_command(
    request: Request, name: str, body: CommandRule, caller: Caller = ADMIN_WRITE
) -> dict[str, Any]:
    """`!cmd`, `!perm` and `!cooldown` with `global`: the default every channel inherits."""
    policy, spec = _policy(request), command_spec(request, name)
    await apply_command_rule(request, GLOBAL, spec, body, caller, caller.rank_in(policy, ""))
    return rule_json(policy, GLOBAL, spec)


@router.delete("/global/commands/{name}")
async def reset_global_command(request: Request, name: str, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    policy, spec = _policy(request), command_spec(request, name)
    body = reset_rule(policy, GLOBAL, spec)
    await apply_command_rule(request, GLOBAL, spec, body, caller, caller.rank_in(policy, ""))
    return rule_json(policy, GLOBAL, spec)


# ── bot-wide word filter ────────────────────────────────────────────────────
@router.get("/global/filters")
async def global_filters(request: Request, caller: Caller = ADMIN_READ) -> dict[str, Any]:
    entries = {e.id: e for e in _state(request, "filters").entries_for(GLOBAL)}  # it lists GLOBAL twice
    return {"filters": [filter_json(e) for _, e in sorted(entries.items())]}


@router.post("/global/filters", status_code=201)
async def add_global_filter(request: Request, body: FilterBody, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    return await add_filter_to(request, GLOBAL, body, caller)


@router.patch("/global/filters/{entry_id}")
async def patch_global_filter(
    request: Request, entry_id: int, body: FilterPatch, caller: Caller = ADMIN_WRITE
) -> dict[str, Any]:
    return await patch_filter_in(request, GLOBAL, entry_id, body, caller)


@router.delete("/global/filters/{entry_id}")
async def remove_global_filter(request: Request, entry_id: int, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    return await remove_filter_from(request, GLOBAL, entry_id, caller)


# ── ignored everywhere ──────────────────────────────────────────────────────
@router.get("/ignored")
async def ignored_everywhere(request: Request, caller: Caller = ADMIN_READ) -> dict[str, Any]:
    """Who the bot ignores in every channel. Add and remove through a channel's route with `everywhere`."""
    return {"ignored": await _ignored_json(request, GLOBAL)}


# ── published everywhere (ADR-0012) ─────────────────────────────────────────
@router.post("/global/publications", status_code=201)
async def publish_global(request: Request, body: PublishBody, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    """`cc publish <name> global`. The list is public: `GET /custom-commands`; packs are `GET /packs`."""
    return await publish_in(request, GLOBAL, body, caller, GLOBAL)


@router.delete("/global/publications/{name}")
async def unpublish_global(request: Request, name: str, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    return await unpublish_in(request, GLOBAL, name, caller)


@router.post("/global/packs", status_code=201)
async def publish_global_pack(request: Request, body: PackRef, caller: Caller = ADMIN_WRITE) -> dict[str, Any]:
    return await publish_pack_in(request, GLOBAL, body, caller)


@router.delete("/global/packs/{name}")
async def unpublish_global_pack(
    request: Request, name: str, owner: str | None = None, caller: Caller = ADMIN_WRITE
) -> dict[str, Any]:
    return await unpublish_pack_in(request, GLOBAL, name, owner, caller)
