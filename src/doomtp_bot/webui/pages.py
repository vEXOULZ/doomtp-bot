"""Server-rendered pages: public docs and the admin UI (architecture §11).

Public pages document every feature and are generated from the same specs the bot runs on, so the docs
can't drift from the code. Admin pages need the local admin password and are LAN-only by default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from doomtp_bot import __version__
from doomtp_bot.api.keys import ApiKeyError
from doomtp_bot.lang import SYNTAX_VERSION
from doomtp_bot.lang.parser import (
    DEFAULT_PREFIX,
    OPERATOR_TOKENS,
    REGISTERED_ROOTS,
    TYPE_NAMES,
    VAR_NAMESPACES,
)
from doomtp_bot.policy.roles import BUILTIN_RANKS, GLOBAL
from doomtp_bot.runtime.preflight import MAX_CC_DEPTH, MAX_INVOCATIONS
from doomtp_bot.runtime.spec import with_sign
from doomtp_bot.webui import emoji
from doomtp_bot.webui.auth import SESSION_COOKIE, AdminAuth

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Every value a template prints goes through here, so the command sign is drawn the same everywhere.
TEMPLATES.env.finalize = emoji.finalize
TEMPLATES.env.globals["emoji"] = emoji.emojify
# The wheel carries a copy beside the static files (see pyproject), because `docs/` isn't installed;
# in a source checkout the repository's own copy is the one being edited, so it wins.
_GRAMMAR_FILES = (
    Path(__file__).resolve().parents[3] / "docs" / "grammar" / "railroad.ebnf",
    Path(__file__).parent / "static" / "railroad.ebnf",
)
_GRAMMAR_FILE = next((f for f in _GRAMMAR_FILES if f.is_file()), _GRAMMAR_FILES[0])
# Read once at import: the docs page shows the same grammar CI checks against the spec (ADR-0011).
GRAMMAR = _GRAMMAR_FILE.read_text(encoding="utf-8") if _GRAMMAR_FILE.is_file() else ""


def _grammar_rules(text: str) -> list[dict[str, str]]:
    """`Name ::= body` per rule, to caption and describe the diagrams drawn by scripts/render_railroad.py.
    An indented line continues the rule above it, the way the file is written."""
    found: list[dict[str, str]] = []
    for line in text.splitlines():
        name, sep, body = line.partition("::=")
        if sep and name.strip() and not line[0].isspace():
            found.append({"name": name.strip(), "body": " ".join(body.split())})
        elif found and line.strip():
            found[-1]["body"] += " " + " ".join(line.split())
    return found


GRAMMAR_RULES = _grammar_rules(GRAMMAR)
router = APIRouter(tags=["web"])


# ── helpers ─────────────────────────────────────────────────────────────────
def _state(request: Request, name: str) -> Any:
    return getattr(request.app.state, name, None)


def _auth(request: Request) -> AdminAuth:
    auth = _state(request, "admin_auth")
    return auth if isinstance(auth, AdminAuth) else AdminAuth()


def _require_admin(request: Request) -> Any:
    auth = _auth(request)
    if not auth.enabled:
        raise HTTPException(status_code=404, detail="the admin UI is disabled (no ADMIN_PASSWORD set)")
    session = auth.session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return session


def _page(request: Request, template: str, **context: Any) -> HTMLResponse:
    session = _auth(request).session(request.cookies.get(SESSION_COOKIE))
    return TEMPLATES.TemplateResponse(
        request=request,
        name=template,
        context={
            "version": __version__,
            "syntax_version": SYNTAX_VERSION,
            "default_prefix": DEFAULT_PREFIX,
            "signed_in": session is not None,
            "csrf": session.csrf if session else "",
            "admin_enabled": _auth(request).enabled,
            **context,
        },
    )


def _channels(request: Request) -> list[Any]:
    policy = _state(request, "policy")
    if policy is None:
        return []
    return sorted(policy.snapshot.channels.values(), key=lambda c: c.login)


def _channel_or_404(request: Request, login: str) -> Any:
    for settings in _channels(request):
        if settings.login == login.lower():
            return settings
    raise HTTPException(status_code=404, detail=f"unknown channel {login}")


# ── public pages ────────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _page(
        request,
        "index.html",
        channels=[c for c in _channels(request) if c.active and c.status == "joined"],
    )


@router.get("/docs/commands", response_class=HTMLResponse)
async def commands_page(request: Request) -> HTMLResponse:
    """Every command the bot offers everywhere: built-ins, plus globally published ones (ADR-0012)."""
    runtime = _state(request, "runtime")
    rows = [_builtin_row(c.spec, DEFAULT_PREFIX) for c in runtime.registry.all()] if runtime else []
    rows += await _global_custom_rows(request)
    rows.sort(key=lambda row: (row["module"], row["name"]))
    return _page(request, "commands.html", rows=rows, modules=sorted({r["module"] for r in rows}))


def _builtin_row(spec: Any, prefix: str) -> dict[str, Any]:
    """One table row. Spec text writes the command sign as `{sign}`; here it becomes a real one."""
    summary = with_sign(spec.summary, prefix)
    return {
        "name": spec.name,
        "usage": spec.usage(),
        "module": spec.module,
        "kind": "built-in",
        "role": spec.required_role,
        "summary": summary,
        "description": with_sign(spec.description, prefix) if spec.description != spec.summary else "",
        "aliases": list(spec.aliases),
        "params": list(spec.params),
        "examples": [example.rendered(prefix) for example in spec.examples],
        "cooldowns": {r: (c.tier_s, c.user_s) for r, c in spec.default_cooldowns.items()},
        "always_on": not spec.toggleable,
        "fixed_policy": spec.fixed_policy,
        "owner": "",
        "version": 0,
        "body": "",
        # Everything the search box matches on, lowercased once here rather than in the browser.
        "search": " ".join([spec.name, *spec.aliases, spec.module, summary]).lower(),
    }


def _custom_row(name: str, command: Any, module: str, kind: str) -> dict[str, Any]:
    from doomtp_bot.customcmds.params import to_params

    summary = command.summary or f"custom command by @{command.owner_login}"
    return {
        "name": name,
        "usage": name,
        "module": module,
        "kind": kind,
        "role": "everyone",
        "summary": summary,
        "description": "",
        "aliases": [],
        "params": list(to_params(command.params)),
        "examples": [],
        "cooldowns": {},
        "always_on": False,
        "fixed_policy": False,
        "owner": command.owner_login,
        "version": command.version,
        "body": command.body,
        "search": " ".join([name, module, summary, command.owner_login, command.body]).lower(),
    }


async def _global_custom_rows(request: Request) -> list[dict[str, Any]]:
    """Derived commands: published to the global scope, so they work in every channel (ADR-0012)."""
    customcmds, packs = _state(request, "customcmds"), _state(request, "packs")
    rows: list[dict[str, Any]] = []
    if customcmds is not None:
        for publication, command in await customcmds.publications_in(GLOBAL):
            if publication.status == "active" and command.status == "active":
                rows.append(_custom_row(publication.name, command, "custom", "derived"))
    if packs is not None:
        for publication, pack in await packs.publications_in(GLOBAL):
            if publication.status != "active":
                continue
            for member in await packs.members(pack.id):
                rows.append(_custom_row(member.name, member, pack.name, "derived"))
    return rows


@router.get("/docs/language", response_class=HTMLResponse)
async def language_page(request: Request) -> HTMLResponse:
    return _page(
        request,
        "language.html",
        operators=list(OPERATOR_TOKENS),
        roots=sorted(REGISTERED_ROOTS),
        types=[*TYPE_NAMES, "choice"],
        namespaces=list(VAR_NAMESPACES),
        limits={"invocations": MAX_INVOCATIONS, "custom command depth": MAX_CC_DEPTH},
        grammar=GRAMMAR,
        grammar_rules=GRAMMAR_RULES,
    )


@router.get("/docs/features", response_class=HTMLResponse)
async def features_page(request: Request) -> HTMLResponse:
    return _page(request, "features.html", roles=sorted(BUILTIN_RANKS.items(), key=lambda kv: kv[1]))


@router.get("/channels/{login}", response_class=HTMLResponse)
async def channel_page(request: Request, login: str) -> HTMLResponse:
    settings = _channel_or_404(request, login)
    customcmds, packs = _state(request, "customcmds"), _state(request, "packs")
    rows: list[dict[str, Any]] = []
    if customcmds is not None:
        for publication, command in await customcmds.publications_in(settings.channel_id):
            if publication.status == "active" and command.status == "active":
                rows.append(_custom_row(publication.name, command, "custom", "published"))
    published_packs = []
    if packs is not None:
        for publication, pack in await packs.publications_in(settings.channel_id, include_global=True):
            if publication.status != "active":
                continue
            members = await packs.members(pack.id)
            published_packs.append(pack)
            rows += [_custom_row(m.name, m, pack.name, "published") for m in members]
    rows.sort(key=lambda row: (row["module"], row["name"]))
    return _page(request, "channel.html", channel=settings, rows=rows, packs=published_packs)


# ── admin ───────────────────────────────────────────────────────────────────
@router.get("/admin/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str = "") -> HTMLResponse:
    if not _auth(request).enabled:
        raise HTTPException(status_code=404, detail="the admin UI is disabled (no ADMIN_PASSWORD set)")
    return _page(request, "login.html", error=error)


@router.post("/admin/login")
async def login(request: Request, password: str = Form("")) -> RedirectResponse:
    auth = _auth(request)
    if not auth.enabled or not auth.check_password(password):
        return RedirectResponse("/admin/login?error=wrong+password", status_code=303)
    session = auth.login()
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(SESSION_COOKIE, session.token, httponly=True, samesite="lax", max_age=int(auth.ttl_s))
    return response


@router.post("/admin/logout")
async def logout(request: Request) -> RedirectResponse:
    _auth(request).logout(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request, new_key: str = "") -> HTMLResponse:
    _require_admin(request)
    health = _state(request, "health")
    _, components = await health.snapshot() if health else (None, {})
    return _page(
        request,
        "admin.html",
        components=components,
        channels=_channels(request),
        audit=await _audit_rows(request),
        api_keys=await _key_rows(request),
        new_key=new_key,
    )


async def _key_rows(request: Request) -> list[dict[str, Any]]:
    keys = _state(request, "api_keys")
    if keys is None:
        return []
    return [
        {
            "id": key.id,
            "name": key.name,
            "scopes": ", ".join(sorted(key.scopes)),
            "created": _when(key.created_at),
            "last_used": _when(key.last_used_at) if key.last_used_at else "never",
        }
        for key in await keys.list()
    ]


def _when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


@router.post("/admin/keys", response_class=HTMLResponse)
async def admin_create_key(
    request: Request, name: str = Form(...), scopes: str = Form("read"), csrf: str = Form("")
) -> HTMLResponse:
    """Create an API key and show it once. It is never redirected through a URL, where it would be logged."""
    _require_csrf(request, csrf)
    keys = _state(request, "api_keys")
    if keys is None:
        raise HTTPException(status_code=503, detail="API keys aren't available")
    try:
        _, secret = await keys.create(name=name, scopes=tuple(s for s in scopes.split(",") if s))
    except ApiKeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await admin_home(request, new_key=secret)


@router.post("/admin/keys/revoke")
async def admin_revoke_key(
    request: Request, key_id: int = Form(...), csrf: str = Form("")
) -> RedirectResponse:
    _require_csrf(request, csrf)
    keys = _state(request, "api_keys")
    if keys is not None:
        await keys.revoke(key_id)
    return RedirectResponse("/admin", status_code=303)


@router.get("/admin/channels/{login}", response_class=HTMLResponse)
async def admin_channel(request: Request, login: str) -> HTMLResponse:
    _require_admin(request)
    settings = _channel_or_404(request, login)
    policy, runtime = _state(request, "policy"), _state(request, "runtime")
    triggers, filters = _state(request, "triggers"), _state(request, "filters")
    customcmds = _state(request, "customcmds")
    modules = sorted({c.spec.module for c in runtime.registry.all()}) if runtime else []
    module_state = []
    if runtime is not None:
        specs = {c.spec.module: c.spec for c in runtime.registry.all()}
        module_state = [
            (module, policy.is_enabled(settings.channel_id, specs[module]), specs[module].toggleable)
            for module in modules
        ]
    publications = await customcmds.publications_in(settings.channel_id) if customcmds else []
    return _page(
        request,
        "admin_channel.html",
        channel=settings,
        modules=module_state,
        triggers=triggers.in_channel(settings.channel_id) if triggers else [],
        filters=filters.entries_for(settings.channel_id) if filters else [],
        publications=publications,
        ignored=sorted(policy.snapshot.ignored.get(settings.channel_id, frozenset())) if policy else [],
    )


@router.post("/admin/channels/{login}/module")
async def admin_toggle_module(
    request: Request, login: str, module: str = Form(...), enabled: str = Form(""), csrf: str = Form("")
) -> RedirectResponse:
    """Turn a module on or off for one channel, through the same service the chat command uses."""
    _require_csrf(request, csrf)
    settings = _channel_or_404(request, login)
    policy = _state(request, "policy")
    from doomtp_bot.policy.repository import Actor

    await policy.mutate(
        lambda repo: repo.set_module_toggle(settings.channel_id, module, enabled == "on", Actor(None, "web"))
    )
    return RedirectResponse(f"/admin/channels/{login}", status_code=303)


@router.post("/admin/channels/{login}/trigger")
async def admin_toggle_trigger(
    request: Request, login: str, trigger_id: int = Form(...), enabled: str = Form(""), csrf: str = Form("")
) -> RedirectResponse:
    _require_csrf(request, csrf)
    settings = _channel_or_404(request, login)
    triggers = _state(request, "triggers")
    await triggers.set_enabled(
        channel_id=settings.channel_id,
        trigger_id=trigger_id,
        enabled=enabled == "on",
        actor_user_id=None,
    )
    return RedirectResponse(f"/admin/channels/{login}", status_code=303)


@router.post("/admin/channels/{login}/filter")
async def admin_toggle_filter(
    request: Request, login: str, entry_id: int = Form(...), enabled: str = Form(""), csrf: str = Form("")
) -> RedirectResponse:
    _require_csrf(request, csrf)
    settings = _channel_or_404(request, login)
    filters = _state(request, "filters")
    await filters.set_enabled(
        channel_id=settings.channel_id, entry_id=entry_id, enabled=enabled == "on", actor_user_id=None
    )
    return RedirectResponse(f"/admin/channels/{login}", status_code=303)


@router.post("/admin/channels/{login}/publication")
async def admin_toggle_publication(
    request: Request, login: str, name: str = Form(...), enabled: str = Form(""), csrf: str = Form("")
) -> RedirectResponse:
    _require_csrf(request, csrf)
    settings = _channel_or_404(request, login)
    customcmds = _state(request, "customcmds")
    await customcmds.set_publication_status(
        channel_id=settings.channel_id,
        name=name,
        status="active" if enabled == "on" else "disabled",
        actor_user_id=None,
        actor_via="web",
    )
    return RedirectResponse(f"/admin/channels/{login}", status_code=303)


def _require_csrf(request: Request, csrf: str) -> None:
    _require_admin(request)
    if not _auth(request).valid_csrf(request.cookies.get(SESSION_COOKIE), csrf):
        raise HTTPException(status_code=403, detail="stale form, please try again")


async def _audit_rows(request: Request, limit: int = 25) -> list[dict[str, Any]]:
    policy = _state(request, "policy")
    if policy is None:
        return []
    async with await policy.repo.conn.execute(
        "SELECT action, channel_id, actor_user_id, target, via, at FROM audit_log ORDER BY id DESC LIMIT %s",
        (limit,),
    ) as cur:
        return [
            {
                "action": r["action"],
                "channel": r["channel_id"] or GLOBAL,
                "actor": r["actor_user_id"] or "system",
                "target": r["target"],
                "via": r["via"],
                "at": r["at"],
            }
            for r in await cur.fetchall()
        ]
