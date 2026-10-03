"""Signing in to the web admin with Twitch (ADR-0017).

`/auth/admin/login` → Twitch → `/auth/admin/callback` → a session cookie → back to the page the user
started from. Unlike the two flows in `auth.py`, nothing is stored in the database: the token only says
who the user is and which channels they moderate, and it lives with their session, in memory.

What a user may do follows from who they are:
  * a bot owner (`BOT_OWNER_IDS`) or a global bot admin is an **admin**, every channel;
  * anyone else is a **moderator** of the joined channels they own or moderate: their own channel if the
    bot is in it, plus Helix `GET /moderation/channels`;
  * someone with none of those is a **user** (ADR-0026): signed in, for what is theirs alone, such as
    adding the bot to their own channel.

Both are worked out again at most every `REFRESH_S`, so a moderator who loses the role loses access
within that window, not at once.

With `SIGNIN_PROVIDER=vexoulz` (ADR-0023) the same flow goes through vexoulz-auth, the sign-in shared by
the vexoulz sites, instead of straight to Twitch: `VexoulzSignIn` below. Someone already signed in on
another of those sites isn't asked again, and signing out everywhere there ends the session here too.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlencode, urlsplit

import aiohttp
import structlog

from doomtp_bot.twitch.auth import AUTHORIZE_URL, STATE_TTL_S, TOKEN_URL, OAuthError, TwitchOAuthHttp

log = structlog.get_logger(__name__)

SIGNIN_SCOPES: tuple[str, ...] = ("user:read:moderated_channels",)
MODERATED_URL = "https://api.twitch.tv/helix/moderation/channels"
REFRESH_S = 300.0
DEFAULT_NEXT = "/admin"

# `no_channels` is no longer produced (ADR-0026: such a user gets a `user` session), but older sites know it.
Reason = Literal["denied", "expired", "twitch", "no_channels"]


class SignInError(Exception):
    """Sign-in failed. `reason` is what the site's login page is told (`?error=<reason>`)."""

    def __init__(self, reason: Reason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class TokenRevoked(OAuthError):
    """Twitch no longer accepts the user's grant: they disconnected the app, or the refresh token is gone."""


class SignInHttp(Protocol):
    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]: ...

    async def validate(self, access_token: str) -> dict[str, Any]: ...

    async def refresh(self, refresh_token: str) -> dict[str, Any]: ...

    async def moderated_channels(self, access_token: str, user_id: str) -> list[str]:
        """Broadcaster ids of every channel the user moderates. Raises TokenRevoked on a 401."""
        ...


class TwitchSignInHttp(TwitchOAuthHttp):
    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        async with aiohttp.ClientSession() as session, session.post(TOKEN_URL, data=data) as resp:
            body: dict[str, Any] = await resp.json()
            if resp.status in (400, 401):
                raise TokenRevoked(f"Twitch refused the refresh token ({resp.status})")
            if resp.status != 200:
                raise OAuthError(f"token refresh failed ({resp.status})")
            return body

    async def moderated_channels(self, access_token: str, user_id: str) -> list[str]:
        headers = {"Authorization": f"Bearer {access_token}", "Client-Id": self.client_id}
        found: list[str] = []
        cursor: str | None = None
        async with aiohttp.ClientSession() as session:
            while True:
                params = {"user_id": user_id, "first": "100", **({"after": cursor} if cursor else {})}
                async with session.get(MODERATED_URL, headers=headers, params=params) as resp:
                    if resp.status == 401:
                        raise TokenRevoked("Twitch refused the user's token")
                    if resp.status != 200:
                        raise OAuthError(f"Get Moderated Channels failed ({resp.status})")
                    body: dict[str, Any] = await resp.json()
                found += [row["broadcaster_id"] for row in body.get("data", [])]
                cursor = (body.get("pagination") or {}).get("cursor")
                if not cursor:
                    return found


@dataclass(slots=True)
class Grant:
    """A signed-in user's token. Refreshed in place when Twitch says it has expired."""

    user_id: str
    login: str
    access_token: str
    refresh_token: str | None
    sid: str | None = None  # the vexoulz-auth session behind it (VexoulzSignIn); None for Twitch's own


@dataclass(frozen=True, slots=True)
class Access:
    role: Literal["admin", "moderator", "user"]
    channels: frozenset[str] | None  # logins; None for an admin, who has every channel


class Policy(Protocol):
    def is_bot_admin(self, user_id: str) -> bool: ...

    def channels(self) -> list[Any]: ...


def access_for(policy: Policy, user_id: str, moderated: Iterable[str]) -> Access:
    """An admin, a moderator of the joined channels that are theirs or that they moderate, or a user who
    manages none of them."""
    if policy.is_bot_admin(user_id):
        return Access("admin", None)
    ids = {user_id, *moderated}
    channels = frozenset(c.login for c in policy.channels() if c.active and c.channel_id in ids)
    return Access("moderator" if channels else "user", channels)


def safe_next(value: str | None) -> str:
    """`value` when it is a path on this site, else `DEFAULT_NEXT`. Never a full URL, and never one a
    browser would read as another host (`//host`, `/\\host`): after sign-in the user must land here."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return DEFAULT_NEXT
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return DEFAULT_NEXT
    parts = urlsplit(value)
    return DEFAULT_NEXT if parts.scheme or parts.netloc else value


class TwitchSignIn:
    def __init__(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        http: SignInHttp,
        policy: Policy,
        clock: Callable[[], float] = time.monotonic,
        refresh_s: float = REFRESH_S,
    ) -> None:
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.http = http
        self.policy = policy
        self.clock = clock
        self.refresh_s = refresh_s
        self._states: dict[str, tuple[float, str]] = {}  # state -> (issued at, where to go after)

    def start(self, next_path: str | None) -> tuple[str, str]:
        """(Twitch's authorize URL, the state). The caller also puts the state in a cookie, so a
        callback is only accepted from the browser that started it."""
        now = self.clock()
        self._states = {s: v for s, v in self._states.items() if now - v[0] < STATE_TTL_S}
        state = secrets.token_urlsafe(24)
        self._states[state] = (now, safe_next(next_path))
        return self.authorize_url(state), state

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": " ".join(SIGNIN_SCOPES),
                "state": state,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    @staticmethod
    def reason_for(error: str) -> Reason:
        """The login page's reason for an `error` the provider sent back."""
        return "denied" if error == "access_denied" else "twitch"

    async def redeem(self, code: str) -> Grant:
        """The user behind an authorization code."""
        token = await self.http.exchange_code(code, self.redirect_uri)
        info = await self.http.validate(token["access_token"])
        return Grant(info["user_id"], info["login"], token["access_token"], token.get("refresh_token"))

    def next_for(self, state: str | None) -> str:
        """Where a callback with this state was headed, even when it failed: the error goes there too."""
        found = self._states.get(state or "")
        return found[1] if found else DEFAULT_NEXT

    async def complete(
        self, code: str | None, state: str | None, error: str | None, browser_state: str | None
    ) -> tuple[Grant, Access, str]:
        """The signed-in user, what they may do, and where to send them. Raises SignInError."""
        found = self._states.pop(state or "", None)
        if error:
            raise SignInError(self.reason_for(error), f"sign-in returned an error: {error}")
        if found is None or self.clock() - found[0] >= STATE_TTL_S:
            raise SignInError("expired", "invalid or expired sign-in state")
        if not browser_state or not secrets.compare_digest(browser_state, state or ""):
            raise SignInError("expired", "this sign-in was started in another browser")
        if not code:
            raise SignInError("twitch", "missing authorization code")
        try:
            grant = await self.redeem(code)
            access = await self.access(grant)
        except (OAuthError, aiohttp.ClientError) as exc:
            raise SignInError("twitch", str(exc)) from exc
        log.info("signin.completed", user=grant.login, role=access.role, channels=sorted(access.channels or ()))
        return grant, access, found[1]

    async def access(self, grant: Grant) -> Access:
        """What `grant`'s user may do now. Asks Helix only for someone who isn't an admin."""
        if self.policy.is_bot_admin(grant.user_id):
            return Access("admin", None)
        try:
            moderated = await self.http.moderated_channels(grant.access_token, grant.user_id)
        except TokenRevoked:
            if not grant.refresh_token:
                raise
            token = await self.http.refresh(grant.refresh_token)  # an expired access token, most likely
            grant.access_token = token["access_token"]
            grant.refresh_token = token.get("refresh_token") or grant.refresh_token
            moderated = await self.http.moderated_channels(grant.access_token, grant.user_id)
        return access_for(self.policy, grant.user_id, moderated)

    async def refresh(self, session: Any) -> bool:
        """Bring a Twitch session's role and channels up to date, at most every `refresh_s`. False when
        the session should end: Twitch (or vexoulz-auth) took the grant back. Someone who manages no
        channel any more stays signed in, as a user.
        A request that merely failed on the way keeps what the session had until the next try."""
        grant = session.grant
        if not isinstance(grant, Grant) or self.clock() - session.checked_at < self.refresh_s:
            return True
        session.checked_at = self.clock()  # before the await: concurrent requests don't all ask Twitch
        try:
            access = await self.access(grant)
        except TokenRevoked:
            log.info("signin.revoked", user=grant.login)
            return False
        except (OAuthError, aiohttp.ClientError, TimeoutError) as exc:
            log.warning("signin.refresh_failed", user=grant.login, error=str(exc))
            return True
        session.role, session.channels = access.role, access.channels
        return True


# ── through vexoulz-auth (ADR-0023) ─────────────────────────────────────────


class VexoulzHttp(Protocol):
    async def token(self, code: str, redirect_uri: str) -> dict[str, Any]:
        """`POST /v1/token`: `{user: {id, login, ...}, sid, expiresAt}`. OAuthError if refused."""
        ...

    async def session_active(self, sid: str) -> bool:
        """`GET /v1/sessions/{sid}`: False once the user signed out (or the session ran out)."""
        ...

    async def moderated_channels(self, user_id: str) -> list[str]:
        """Broadcaster ids the user moderates, asked of Twitch by vexoulz-auth with the token it keeps.
        TokenRevoked when there is no usable token any more: the user must sign in again."""
        ...


class VexoulzAuthHttp:
    """vexoulz-auth's backend API, authenticated as a registered client (HTTP Basic)."""

    def __init__(self, base_url: str, client_id: str, client_secret: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = aiohttp.BasicAuth(client_id, client_secret)
        self.timeout = aiohttp.ClientTimeout(total=15)

    async def _call(self, method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        async with (
            aiohttp.ClientSession(auth=self.auth, timeout=self.timeout) as session,
            session.request(method, self.base_url + path, **kwargs) as resp,
        ):
            try:
                body: dict[str, Any] = await resp.json(content_type=None)
            except ValueError:
                body = {}
            return resp.status, body

    async def token(self, code: str, redirect_uri: str) -> dict[str, Any]:
        status, body = await self._call("POST", "/v1/token", json={"code": code, "redirect_uri": redirect_uri})
        if status != 200:
            raise OAuthError(f"vexoulz-auth refused the code ({status} {body.get('error')})")
        return body

    async def session_active(self, sid: str) -> bool:
        status, body = await self._call("GET", f"/v1/sessions/{sid}")
        if status == 404:
            return False
        if status != 200:
            raise OAuthError(f"vexoulz-auth session check failed ({status})")
        return bool(body.get("active"))

    async def moderated_channels(self, user_id: str) -> list[str]:
        status, body = await self._call("GET", f"/v1/users/{user_id}/moderated-channels")
        if status in (404, 409, 410):  # never signed in here, the scope is gone, or Twitch took it back
            raise TokenRevoked(f"vexoulz-auth has no usable token ({status} {body.get('error')})")
        if status != 200:
            raise OAuthError(f"vexoulz-auth moderated-channels failed ({status})")
        return [str(c) for c in body.get("channels", [])]


class VexoulzSignIn(TwitchSignIn):
    """The same flow, states and outcomes as TwitchSignIn, with vexoulz-auth in Twitch's place.

    `/authorize` answers at once for someone already signed in on another vexoulz site, and asks Twitch
    (once) for the moderated-channels scope. Every refresh also checks the vexoulz-auth session, so
    signing out everywhere ends this one within `REFRESH_S`.
    """

    http: VexoulzHttp  # type: ignore[assignment]

    def __init__(
        self,
        *,
        auth_url: str,
        client_id: str,
        redirect_uri: str,
        http: VexoulzHttp,
        policy: Policy,
        clock: Callable[[], float] = time.monotonic,
        refresh_s: float = REFRESH_S,
    ) -> None:
        super().__init__(
            client_id=client_id,
            redirect_uri=redirect_uri,
            http=http,  # type: ignore[arg-type]
            policy=policy,
            clock=clock,
            refresh_s=refresh_s,
        )
        self.auth_url = auth_url.rstrip("/")

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(SIGNIN_SCOPES),
                "state": state,
            }
        )
        return f"{self.auth_url}/authorize?{query}"

    @staticmethod
    def reason_for(error: str) -> Reason:
        return "denied" if error == "denied" else "expired" if error == "expired" else "twitch"

    async def redeem(self, code: str) -> Grant:
        body = await self.http.token(code, self.redirect_uri)
        user = body["user"]
        return Grant(str(user["id"]), str(user["login"]), "", None, sid=str(body["sid"]))

    async def access(self, grant: Grant) -> Access:
        if grant.sid is None or not await self.http.session_active(grant.sid):
            raise TokenRevoked("signed out of vexoulz-auth")
        if self.policy.is_bot_admin(grant.user_id):
            return Access("admin", None)
        return access_for(self.policy, grant.user_id, await self.http.moderated_channels(grant.user_id))
