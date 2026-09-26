"""Who is calling the API, and what they may use (architecture §11, ADR-0017).

Three kinds of caller:
  * an **API key** (`read` or `write`), for scripts: an admin within its scopes;
  * the **password session**: an admin;
  * a **Twitch session** (ADR-0017): an admin when the user is a bot owner or bot admin, otherwise a
    moderator of the channels in the session.

Every private route says which **area** it belongs to. `channel` routes are open to moderators in the
channels they manage; `admin` routes are not open to moderators at all. The check is here, on the
server, because the site only hides what a caller can't use.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, HTTPException, Request

from doomtp_bot.policy.repository import Actor
from doomtp_bot.webui.auth import SESSION_COOKIE, AdminAuth, Session

Area = Literal["channel", "admin"]
API_ACTOR = Actor(None, "api")

# The chat part of a channel's settings, which a moderator may change (ADR-0017). The rest (logging,
# backfill and the "who may" roles) are for admins.
MODERATOR_SETTABLE = frozenset(
    {
        "prefix",
        "quiet_errors",
        "cc_edit_notice",
        "reply_hold_ms",
        "timezone",
        "automod_action",
        "automod_timeout_s",
    }
)


@dataclass(frozen=True, slots=True)
class Caller:
    label: str  # for the logs: "key:<name>", "session" or "user:<login>"
    actor: Actor  # what the audit log records for this caller's writes
    role: Literal["admin", "moderator"] = "admin"
    user_id: str | None = None
    channels: frozenset[str] | None = None  # logins a moderator manages; None means every channel

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def manages(self, login: str) -> bool:
        return self.channels is None or login.lower().lstrip("#") in self.channels


def caller_for_session(session: Session) -> Caller:
    if session.user_id is None:  # the password
        return Caller("session", API_ACTOR)
    role: Literal["admin", "moderator"] = "admin" if session.is_admin else "moderator"
    return Caller(
        f"user:{session.user_login or session.user_id}",
        Actor(session.user_id, "web"),
        role=role,
        user_id=session.user_id,
        channels=None if session.is_admin else session.channels,
    )


async def current_session(request: Request) -> Session | None:
    """The request's session, with a Twitch sign-in's role and channels brought up to date first (at most
    every few minutes, `twitch/signin.py`). A user Twitch no longer vouches for is signed out here."""
    auth: AdminAuth = request.app.state.admin_auth
    token = request.cookies.get(SESSION_COOKIE)
    session = auth.session(token)
    signin = getattr(request.app.state, "twitch_signin", None)
    if session is None or session.grant is None or signin is None:
        return session
    if not await signin.refresh(session):
        auth.logout(token)
        return None
    return session


def _state(request: Request, name: str) -> Any:
    found = getattr(request.app.state, name, None)
    if found is None:
        raise HTTPException(status_code=503, detail=f"{name} isn't available")
    return found


async def authenticate(request: Request, scope: str) -> Caller:
    """Who is calling. Raises 401/403 when they may not use `scope` at all."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        keys = _state(request, "api_keys")
        key = await keys.verify(header[7:].strip())
        if key is None:
            raise HTTPException(status_code=401, detail="unknown or revoked API key")
        if not key.allows(scope):
            raise HTTPException(status_code=403, detail=f"this key has no {scope} scope")
        return Caller(f"key:{key.name}", API_ACTOR)
    auth: AdminAuth = _state(request, "admin_auth")
    token = request.cookies.get(SESSION_COOKIE)
    session = await current_session(request)
    if session is not None:
        if scope != "read" and not auth.valid_csrf(token, request.headers.get("x-csrf-token")):
            raise HTTPException(status_code=403, detail="a session write needs the X-CSRF-Token header")
        return caller_for_session(session)
    raise HTTPException(
        status_code=401,
        detail="an API key or an admin session is required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def check_area(caller: Caller, area: Area, login: str | None) -> None:
    """403 unless the caller may use this area, in this channel when the route names one."""
    if caller.is_admin:
        return
    if area == "admin":
        raise HTTPException(status_code=403, detail="only an admin can use this")
    if login is not None and not caller.manages(login):
        raise HTTPException(status_code=403, detail=f"you don't moderate {login}")


def require(scope: str, area: Area, *, check_channel: bool = True) -> Callable[[Request], Awaitable[Caller]]:
    """A dependency: the caller, once they may use `scope` in `area`. With `check_channel`, a moderator
    must also manage the channel named by the route's `{login}`. A route that turns it off checks for
    itself (removing one's own self-ignore is allowed anywhere)."""

    async def dependency(request: Request) -> Caller:
        caller = await authenticate(request, scope)
        check_area(caller, area, request.path_params.get("login") if check_channel else None)
        return caller

    dependency.area = area  # type: ignore[attr-defined]  # read by the test that walks every route
    return dependency


READ = Depends(require("read", "channel"))
WRITE = Depends(require("write", "channel"))
ADMIN_READ = Depends(require("read", "admin"))
ADMIN_WRITE = Depends(require("write", "admin"))
# The route checks the channel itself: a user may lift their own self-ignore anywhere.
WRITE_OWN = Depends(require("write", "channel", check_channel=False))
