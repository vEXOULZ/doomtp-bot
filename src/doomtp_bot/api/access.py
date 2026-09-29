"""Who is calling the API, and what they may use (architecture §11, ADR-0017, ADR-0026).

Three kinds of caller:
  * an **API key** (`read` or `write`), for scripts: an admin within its scopes;
  * the **password session**: an admin;
  * a **Twitch session** (ADR-0017): an admin when the user is a bot owner or bot admin, otherwise a
    moderator of the channels in the session, or a user who manages none (ADR-0026).

Every private route says which **area** it belongs to. `personal` routes are open to anyone signed in;
`channel` routes to those who manage the channel, at the route's minimum rank, which is the rank chat
would give them there (ADR-0026); `admin` routes to admins only. The check is here, on the server,
because the site only hides what a caller can't use.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, HTTPException, Request

from doomtp_bot.api.sessions import SESSION_COOKIE, AdminAuth, Session
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.roles import BOT_ADMIN_RANK, BOT_OWNER_RANK, BROADCASTER_RANK, MODERATOR_RANK

Area = Literal["personal", "channel", "admin"]
Role = Literal["admin", "moderator", "user"]
ChannelRole = Literal["broadcaster", "moderator"]
API_ACTOR = Actor(None, "api")

# The lowest rank that may change each channel setting, as in chat (ADR-0026): the chat settings are for
# moderators; logging, backfill and the "who may" roles for the broadcaster.
SETTING_RANKS: dict[str, int] = {
    "prefix": MODERATOR_RANK,
    "quiet_errors": MODERATOR_RANK,
    "cc_edit_notice": MODERATOR_RANK,
    "reply_hold_ms": MODERATOR_RANK,
    "timezone": MODERATOR_RANK,
    "automod_action": MODERATOR_RANK,
    "automod_timeout_s": MODERATOR_RANK,
    "public_log": MODERATOR_RANK,
    "log_enabled": BROADCASTER_RANK,
    "history_backfill": BROADCASTER_RANK,
    "channel_var_write_role": BROADCASTER_RANK,
    "grant_min_role": BROADCASTER_RANK,
    "publish_min_role": BROADCASTER_RANK,
    "create_min_role": BROADCASTER_RANK,
    "var_admin_role": BROADCASTER_RANK,
}
RANK_NAMES = {MODERATOR_RANK: "a moderator", BROADCASTER_RANK: "the broadcaster", BOT_ADMIN_RANK: "an admin"}


@dataclass(frozen=True, slots=True)
class Caller:
    label: str  # for the logs: "key:<name>", "session", "user:<login>" or "public"
    actor: Actor  # what the audit log records for this caller's writes
    role: Role = "admin"
    user_id: str | None = None
    channels: frozenset[str] | None = None  # logins a moderator manages; None means every channel
    login: str | None = None  # the signed-in Twitch user's login

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def manages(self, login: str) -> bool:
        return self.channels is None or login.lower().lstrip("#") in self.channels

    def channel_role(self, policy: Any, login: str) -> ChannelRole | None:
        """Why the caller manages `login`: it is their own channel, or they moderate it. None for a
        channel they don't manage, and for an admin, who manages every channel as an admin."""
        if self.is_admin or self.user_id is None or not self.manages(login):
            return None
        settings = policy.channel_by_login(login)
        return "broadcaster" if settings is not None and settings.channel_id == self.user_id else "moderator"

    def rank_in(self, policy: Any, login: str) -> int:
        """The rank chat would give the caller in `login` (ADR-0026): their Twitch role there as a badge,
        raised by any custom role they hold. An admin has the bot-admin rank (a bot owner, the owner's)
        everywhere; a channel the caller doesn't manage gives 0."""
        if self.is_admin:
            return BOT_OWNER_RANK if self.user_id in getattr(policy, "owners", ()) else BOT_ADMIN_RANK
        role = self.channel_role(policy, login)
        settings = policy.channel_by_login(login)
        if role is None or settings is None or self.user_id is None:
            return 0
        chatter = policy.build_chatter(
            settings.channel_id, self.user_id, self.login or "", badges=frozenset({role})
        )
        return int(chatter.rank)


def caller_for_session(session: Session) -> Caller:
    if session.user_id is None:  # the password
        return Caller("session", API_ACTOR)
    role: Role = "admin" if session.is_admin else "user" if session.role == "user" else "moderator"
    return Caller(
        f"user:{session.user_login or session.user_id}",
        Actor(session.user_id, "web"),
        role=role,
        user_id=session.user_id,
        channels=None if session.is_admin else session.channels,
        login=session.user_login,
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
    if caller.is_admin or area == "personal":
        return
    if area == "admin":
        raise HTTPException(status_code=403, detail="only an admin can use this")
    if login is not None and not caller.manages(login):
        raise HTTPException(status_code=403, detail=f"you don't moderate {login}")


def check_rank(request: Request, caller: Caller, login: str, min_rank: int, what: str = "do this") -> None:
    """403 unless the caller's rank in `login` reaches `min_rank` (ADR-0026). An unknown channel passes:
    the route answers 404 for it."""
    policy = getattr(request.app.state, "policy", None)
    if caller.is_admin or policy is None or policy.channel_by_login(login) is None:
        return
    if caller.rank_in(policy, login) < min_rank:
        who = RANK_NAMES.get(min_rank, f"rank {min_rank}")
        raise HTTPException(status_code=403, detail=f"only {who} can {what} in {login}")


def require(
    scope: str, area: Area, *, check_channel: bool = True, min_rank: int = MODERATOR_RANK
) -> Callable[[Request], Awaitable[Caller]]:
    """A dependency: the caller, once they may use `scope` in `area`. With `check_channel`, a caller
    must also manage the channel named by the route's `{login}`, at `min_rank` there. A route that turns
    it off checks for itself (removing one's own self-ignore is allowed anywhere)."""

    async def dependency(request: Request) -> Caller:
        caller = await authenticate(request, scope)
        login = request.path_params.get("login") if check_channel else None
        check_area(caller, area, login)
        if area == "channel" and login is not None and min_rank > MODERATOR_RANK:
            check_rank(request, caller, login, min_rank)
        return caller

    # Read by the test that walks every route.
    dependency.area = area  # type: ignore[attr-defined]
    dependency.min_rank = min_rank if area == "channel" else None  # type: ignore[attr-defined]
    return dependency


READ = Depends(require("read", "channel"))
WRITE = Depends(require("write", "channel"))
BROADCASTER_WRITE = Depends(require("write", "channel", min_rank=BROADCASTER_RANK))
PERSONAL_READ = Depends(require("read", "personal"))
PERSONAL_WRITE = Depends(require("write", "personal"))
ADMIN_READ = Depends(require("read", "admin"))
ADMIN_WRITE = Depends(require("write", "admin"))
# The route checks the channel itself: a user may lift their own self-ignore anywhere.
WRITE_OWN = Depends(require("write", "channel", check_channel=False))
