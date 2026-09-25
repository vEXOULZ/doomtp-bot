"""`/api/v1/session` and `/api/v1/keys`: the admin login and API keys, for a UI served elsewhere (ADR-0016).

The same password and the same in-memory sessions as the server-rendered admin pages (`webui/auth.py`), so
either UI can log in and both see the result. The session cookie is `HttpOnly`; the browser learns the
CSRF token from `GET /session` instead and sends it back in `X-CSRF-Token` on every change.

API keys are managed with a session only. A key that could mint keys would turn one leaked `write` key
into a permanent one, and scripts have no business creating credentials.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from doomtp_bot.api.keys import SCOPES, ApiKey, ApiKeyError, ApiKeyService
from doomtp_bot.webui.auth import SESSION_COOKIE, AdminAuth, LoginLimiter, Session

router = APIRouter(prefix="/api/v1", tags=["session"])


def _auth(request: Request) -> AdminAuth:
    return request.app.state.admin_auth  # type: ignore[no-any-return]


def _limiter(request: Request) -> LoginLimiter:
    return request.app.state.login_limiter  # type: ignore[no-any-return]


def client_address(request: Request) -> str:
    """The caller's address. Behind a proxy it is right only when uvicorn trusts that proxy's
    forwarded headers (`WEB_FORWARDED_ALLOW_IPS`); otherwise every login shares the proxy's address."""
    return request.client.host if request.client else "unknown"


def _session_json(auth: AdminAuth, session: Session | None) -> dict[str, Any]:
    return {
        "authenticated": session is not None,
        "csrf": session.csrf if session else None,
        "expires_at": int((time.time() + auth.expires_in(session)) * 1000) if session else None,
        "admin_enabled": auth.enabled,
    }


def require_session(request: Request, *, write: bool) -> Session:
    """The caller's admin session, or 401. A write also needs the session's CSRF token."""
    auth = _auth(request)
    token = request.cookies.get(SESSION_COOKIE)
    session = auth.session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="an admin session is required")
    if write and not auth.valid_csrf(token, request.headers.get("x-csrf-token")):
        raise HTTPException(status_code=403, detail="a session write needs the X-CSRF-Token header")
    return session


# ── session ─────────────────────────────────────────────────────────────────
class Login(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


@router.get("/session")
async def get_session(request: Request) -> dict[str, Any]:
    auth = _auth(request)
    return _session_json(auth, auth.session(request.cookies.get(SESSION_COOKIE)))


@router.post("/session")
async def login(request: Request, body: Login, response: Response) -> dict[str, Any]:
    auth, limiter, address = _auth(request), _limiter(request), client_address(request)
    if not auth.enabled:
        raise HTTPException(status_code=404, detail="the admin UI is disabled (no ADMIN_PASSWORD set)")
    wait = limiter.retry_after(address)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail="too many failed logins; try again later",
            headers={"Retry-After": str(wait)},
        )
    # scrypt takes tens of milliseconds by design; off the event loop, so chat keeps flowing meanwhile.
    if not await asyncio.to_thread(auth.check_password, body.password):
        limiter.failed(address)
        raise HTTPException(status_code=401, detail="wrong password")
    limiter.reset(address)
    auth.logout(request.cookies.get(SESSION_COOKIE))
    session = auth.login()
    response.set_cookie(
        SESSION_COOKIE,
        session.token,
        max_age=int(auth.ttl_s),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return _session_json(auth, session)


@router.delete("/session", status_code=204)
async def logout(request: Request) -> Response:
    auth = _auth(request)
    token = request.cookies.get(SESSION_COOKIE)
    if auth.session(token) is not None:
        require_session(request, write=True)  # a cross-site page must not be able to log you out
        auth.logout(token)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ── API keys ────────────────────────────────────────────────────────────────
class NewKey(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(default_factory=lambda: ["read"], min_length=1, max_length=len(SCOPES))


def _keys(request: Request) -> ApiKeyService:
    keys = getattr(request.app.state, "api_keys", None)
    if keys is None:
        raise HTTPException(status_code=503, detail="API keys aren't available")
    return keys  # type: ignore[no-any-return]


def _key_json(key: ApiKey) -> dict[str, Any]:
    return {
        "id": key.id,
        "name": key.name,
        "scopes": sorted(key.scopes),
        "created_at": key.created_at,
        "last_used_at": key.last_used_at,
    }


@router.get("/keys")
async def list_keys(request: Request) -> dict[str, Any]:
    require_session(request, write=False)
    return {"keys": [_key_json(k) for k in await _keys(request).list()]}


@router.post("/keys", status_code=201)
async def create_key(request: Request, body: NewKey) -> dict[str, Any]:
    """The key's secret is in this response and nowhere else: it is stored only as a hash."""
    require_session(request, write=True)
    try:
        key, secret = await _keys(request).create(name=body.name, scopes=tuple(body.scopes))
    except ApiKeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**_key_json(key), "secret": secret}


@router.delete("/keys/{key_id}")
async def revoke_key(request: Request, key_id: int) -> dict[str, Any]:
    require_session(request, write=True)
    if not await _keys(request).revoke(key_id):
        raise HTTPException(status_code=404, detail=f"no active key {key_id}")
    return {"id": key_id, "revoked": True}
