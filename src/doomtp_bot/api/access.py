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

An admin session may send `X-View-As` to preview the API as another viewer (ADR-0030): reads are
answered as that viewer would get them, and every write is refused while the header is there.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from fastapi import Depends, HTTPException, Request
from vex_platform.actor import VIAS, Via
from vex_platform.actor import Actor as PlatformActor

from doomtp_bot.api.sessions import SESSION_COOKIE, AdminAuth, Session
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.roles import (
    BOT_ADMIN_RANK,
    BOT_OWNER_RANK,
    BROADCASTER_RANK,
    CUSTOM_RANK_MAX,
    CUSTOM_RANK_MIN,
    MODERATOR_RANK,
)

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


VIEW_AS_HEADER = "X-View-As"
ViewAsKind = Literal["signed-out", "user", "moderator", "broadcaster", "custom"]


@dataclass(frozen=True, slots=True)
class ViewAs:
    """Who an admin previews the API as (`X-View-As`, ADR-0030): someone signed out, a user who manages
    no channel, or someone with a rank in one channel (a moderator, the broadcaster, or the holder of a
    custom role ranked 1-99 who is not a Twitch moderator there)."""

    kind: ViewAsKind
    channel: str | None = None  # the channel's login, for the kinds with a rank in one
    rank: int = 0

    @property
    def header(self) -> str:
        """The header's value for this preview, as the bot echoes it back."""
        if self.channel is None:
            return self.kind
        what = str(self.rank) if self.kind == "custom" else self.kind
        return f"{what}@{self.channel}"

    @property
    def manages(self) -> bool:
        """Whether this viewer's session would list the channel: its moderators and broadcaster only. A
        custom role raises a rank but, as for a real sign-in, doesn't make a channel theirs to manage."""
        return self.kind in ("moderator", "broadcaster")

    def describe(self) -> str:
        match self.kind:
            case "signed-out":
                return "someone signed out"
            case "user":
                return "a user who manages no channel"
            case "moderator":
                return f"a moderator of {self.channel}"
            case "broadcaster":
                return f"the broadcaster of {self.channel}"
            case _:
                return f"someone with a rank {self.rank} role in {self.channel}"


def parse_view_as(value: str) -> ViewAs:
    """`signed-out`, `user`, `moderator@<login>`, `broadcaster@<login>` or `<rank>@<login>` for a custom
    role ranked 1-99. 400 for anything else."""
    text = value.strip().lower()
    if text in ("signed-out", "user"):
        return ViewAs(cast(ViewAsKind, text))
    what, at, channel = text.partition("@")
    channel = channel.lstrip("#")
    if at and channel and channel.replace("_", "").isalnum():
        if what == "moderator":
            return ViewAs("moderator", channel, MODERATOR_RANK)
        if what == "broadcaster":
            return ViewAs("broadcaster", channel, BROADCASTER_RANK)
        if what.isdigit() and CUSTOM_RANK_MIN <= int(what) <= CUSTOM_RANK_MAX:
            return ViewAs("custom", channel, int(what))
    raise HTTPException(
        status_code=400,
        detail=f"{VIEW_AS_HEADER} is signed-out, user, moderator@<channel>, broadcaster@<channel>"
        f" or <rank {CUSTOM_RANK_MIN}-{CUSTOM_RANK_MAX}>@<channel>",
    )


@dataclass(frozen=True, slots=True)
class Caller:
    label: str  # for the logs: "key:<name>", "session", "user:<login>" or "public"
    actor: Actor  # what the audit log records for this caller's writes
    role: Role = "admin"
    user_id: str | None = None
    channels: frozenset[str] | None = None  # logins a moderator manages; None means every channel
    login: str | None = None  # the signed-in Twitch user's login
    view_as: ViewAs | None = None  # an admin previewing as someone else (ADR-0030); reads only

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def manages(self, login: str) -> bool:
        return self.channels is None or login.lower().lstrip("#") in self.channels

    def channel_role(self, policy: Any, login: str) -> ChannelRole | None:
        """Why the caller manages `login`: it is their own channel, or they moderate it. None for a
        channel they don't manage, and for an admin, who manages every channel as an admin."""
        if self.view_as is not None:
            view = self.view_as
            here = view.channel == login.lower().lstrip("#")
            return cast(ChannelRole, view.kind) if here and view.manages else None
        if self.is_admin or self.user_id is None or not self.manages(login):
            return None
        settings = policy.channel_by_login(login)
        return "broadcaster" if settings is not None and settings.channel_id == self.user_id else "moderator"

    def is_broadcaster_of(self, settings: Any) -> bool:
        """Whether the channel is the caller's own (for a preview: whether it previews its broadcaster)."""
        if self.view_as is not None:
            return self.view_as.kind == "broadcaster" and self.view_as.channel == settings.login
        return self.user_id is not None and self.user_id == settings.channel_id

    def rank_in(self, policy: Any, login: str) -> int:
        """The rank chat would give the caller in `login` (ADR-0026): their Twitch role there as a badge,
        raised by any custom role they hold. An admin has the bot-admin rank (a bot owner, the owner's)
        everywhere. In a channel the caller doesn't manage only a custom role counts (a subscriber or VIP
        badge isn't known outside chat); an unknown channel, or a caller without a user, gives 0. A
        preview has its rank in its channel and none anywhere else."""
        if self.view_as is not None:
            return self.view_as.rank if self.view_as.channel == login.lower().lstrip("#") else 0
        if self.is_admin:
            return BOT_OWNER_RANK if self.user_id in getattr(policy, "owners", ()) else BOT_ADMIN_RANK
        role = self.channel_role(policy, login)
        settings = policy.channel_by_login(login)
        if settings is None or self.user_id is None:
            return 0
        badges = frozenset({role}) if role is not None else frozenset[str]()
        chatter = policy.build_chatter(settings.channel_id, self.user_id, self.login or "", badges=badges)
        return int(chatter.rank)


def previewing(caller: Caller, view: ViewAs) -> Caller:
    """The caller an admin previews as: that viewer's role and channels, still under the admin's own
    user and actor, so their own changes and personal pages stay theirs."""
    return replace(
        caller,
        label=f"{caller.label} as {view.header}",
        role="moderator" if view.manages else "user",
        channels=frozenset({view.channel}) if view.manages and view.channel else frozenset(),
        view_as=view,
    )


def view_as(request: Request, session: Session | None) -> ViewAs | None:
    """The request's `X-View-As`, or None without one. 403 unless it comes with an admin session, and
    400 when the channel it names isn't one the bot knows."""
    value = request.headers.get(VIEW_AS_HEADER)
    if value is None:
        return None
    if session is None or not session.is_admin:
        raise HTTPException(status_code=403, detail=f"only an admin session can send {VIEW_AS_HEADER}")
    view = parse_view_as(value)
    policy = getattr(request.app.state, "policy", None)
    if view.channel is not None and policy is not None:
        settings = policy.channel_by_login(view.channel)
        if settings is None:
            raise HTTPException(status_code=400, detail=f"{VIEW_AS_HEADER}: no channel named {view.channel}")
        view = replace(view, channel=settings.login)
    return view


def refuse_writes(view: ViewAs) -> HTTPException:
    """The refusal for any change asked under `X-View-As`."""
    return HTTPException(
        status_code=403,
        detail=f"read-only while viewing as {view.describe()}; drop {VIEW_AS_HEADER} to change anything",
    )


def platform_actor(caller: Caller) -> PlatformActor:
    """The caller as vex-platform names actors, for the job runtime and the shared audit table (ADR-0027)."""
    via = cast(Via, caller.actor.via if caller.actor.via in VIAS else "api")
    if caller.user_id is not None:
        return PlatformActor("user", caller.user_id, caller.login, via)
    if caller.label.startswith("key:"):
        return PlatformActor("api_key", caller.label.removeprefix("key:"), via=via)
    return PlatformActor("user", None, caller.label, via)  # the admin password's session


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
        if VIEW_AS_HEADER in request.headers:
            raise HTTPException(status_code=400, detail=f"{VIEW_AS_HEADER} needs an admin session, not a key")
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
        view = view_as(request, session)
        if view is not None and scope != "read":
            raise refuse_writes(view)
        if scope != "read" and not auth.valid_csrf(token, request.headers.get("x-csrf-token")):
            raise HTTPException(status_code=403, detail="a session write needs the X-CSRF-Token header")
        if view is None:
            return caller_for_session(session)
        if view.kind == "signed-out":
            # Marked, so the site can tell it from its session ending.
            raise HTTPException(
                status_code=401,
                detail="viewing as someone signed out",
                headers={VIEW_AS_HEADER: view.header},
            )
        return previewing(caller_for_session(session), view)
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


def check_setting_role(request: Request, caller: Caller, login: str, setting: str, what: str) -> None:
    """403 unless the caller reaches the role a channel setting names (`publish_min_role` and the like),
    as `cc` and `!var` check it in chat; a bot admin always does. An unknown channel passes (404 later)."""
    policy = getattr(request.app.state, "policy", None)
    settings = None if policy is None else policy.channel_by_login(login)
    if caller.is_admin or policy is None or settings is None:
        return
    role = getattr(settings, setting)
    required = policy.rank_of(settings.channel_id, role)
    if required is None or caller.rank_in(policy, login) < required:
        raise HTTPException(status_code=403, detail=f"only {role} or above can {what} in {settings.login}")


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
