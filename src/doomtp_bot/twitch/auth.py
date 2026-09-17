"""One-time OAuth authorization-code flow for the bot account, served by our FastAPI app (architecture §3.1).

GET /auth/login → Twitch consent page → GET /auth/callback → token stored in bot.db → Twitch client (re)starts.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import aiohttp

from doomtp_bot.twitch.tokens import BOT_IDENTITY, TokenStore

AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

# Basic tier plus moderator actions (ADR-0007). Moderator scopes only take effect where the bot is a mod.
BOT_SCOPES: tuple[str, ...] = (
    "user:read:chat",
    "user:write:chat",
    "user:bot",
    "moderator:read:followers",
    "moderator:manage:banned_users",
    "moderator:manage:chat_messages",
)
STATE_TTL_S = 600


class OAuthError(Exception):
    pass


class OAuthHttp(Protocol):
    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]: ...

    async def validate(self, access_token: str) -> dict[str, Any]: ...


class TwitchOAuthHttp:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        async with aiohttp.ClientSession() as session, session.post(TOKEN_URL, data=data) as resp:
            body: dict[str, Any] = await resp.json()
            if resp.status != 200:
                raise OAuthError(f"token exchange failed ({resp.status}): {body.get('message', body)}")
            return body

    async def validate(self, access_token: str) -> dict[str, Any]:
        headers = {"Authorization": f"OAuth {access_token}"}
        async with aiohttp.ClientSession() as session, session.get(VALIDATE_URL, headers=headers) as resp:
            body: dict[str, Any] = await resp.json()
            if resp.status != 200:
                raise OAuthError(f"token validation failed ({resp.status})")
            return body


@dataclass(frozen=True, slots=True)
class AuthorizedAccount:
    user_id: str
    login: str
    scopes: tuple[str, ...]


class TwitchAuth:
    def __init__(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        tokens: TokenStore,
        http: OAuthHttp,
        on_bot_authorized: Callable[[AuthorizedAccount], Awaitable[None]] | None = None,
        expected_bot_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.expected_bot_id = expected_bot_id
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.tokens = tokens
        self.http = http
        self.on_bot_authorized = on_bot_authorized
        self.clock = clock
        self._states: dict[str, float] = {}

    def login_url(self) -> str:
        now = self.clock()
        self._states = {s: t for s, t in self._states.items() if now - t < STATE_TTL_S}
        state = secrets.token_urlsafe(24)
        self._states[state] = now
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": " ".join(BOT_SCOPES),
                "state": state,
                "force_verify": "true",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def complete(
        self, code: str | None, state: str | None, error: str | None = None
    ) -> AuthorizedAccount:
        if error:
            raise OAuthError(f"Twitch returned an error: {error}")
        issued = self._states.pop(state or "", None)
        if issued is None or self.clock() - issued >= STATE_TTL_S:
            raise OAuthError("invalid or expired login state; start again from /auth/login")
        if not code:
            raise OAuthError("missing authorization code")
        token = await self.http.exchange_code(code, self.redirect_uri)
        info = await self.http.validate(token["access_token"])
        if self.expected_bot_id and info["user_id"] != self.expected_bot_id:
            raise OAuthError(
                f"signed in as {info.get('login')} ({info['user_id']}), but the bot account is"
                f" {self.expected_bot_id}. Log out of Twitch and sign in as the bot account."
            )
        scopes = tuple(info.get("scopes") or token.get("scope") or ())
        missing = [s for s in ("user:read:chat", "user:write:chat") if s not in scopes]
        if missing:
            raise OAuthError(f"required scopes were not granted: {', '.join(missing)}")
        await self.tokens.save(
            identity=BOT_IDENTITY,
            user_id=info["user_id"],
            login=info["login"],
            access_token=token["access_token"],
            refresh_token=token.get("refresh_token"),
            scopes=scopes,
            expires_in=token.get("expires_in"),
        )
        account = AuthorizedAccount(info["user_id"], info["login"], scopes)
        if self.on_bot_authorized is not None:
            await self.on_bot_authorized(account)
        return account
