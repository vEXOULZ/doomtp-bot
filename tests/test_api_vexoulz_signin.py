"""Signing in to the web admin through vexoulz-auth (ADR-0023): same routes and sessions as ADR-0017."""
# ruff: noqa: F811  (the imported fixtures are parameters here, which ruff reads as redefinitions)

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.api.sessions import SESSION_COOKIE
from doomtp_bot.policy.repository import Actor
from doomtp_bot.twitch.auth import OAuthError
from doomtp_bot.twitch.signin import REFRESH_S, TokenRevoked, VexoulzSignIn
from tests.test_api_data import (  # noqa: F401  (fixtures)
    CHANNEL_ID,
    CHANNEL_LOGIN,
    app_and_keys,
    client,
)
from tests.test_api_signin import OWN_ID, OWN_LOGIN, PARTED_ID, error_of

MOD_ID = "300"
USERS = {"mod": (MOD_ID, "mod"), "owner": ("1", "owner"), "nobody": ("9", "nobody")}


class FakeVexoulzHttp:
    """vexoulz-auth's backend API: codes, sessions, and the moderated channels it asks Twitch for."""

    def __init__(self) -> None:
        self.moderated: dict[str, list[str]] = {MOD_ID: [CHANNEL_ID, PARTED_ID]}
        self.signed_out: set[str] = set()
        self.revoked: set[str] = set()  # users whose Twitch token vexoulz-auth can no longer use
        self.fail = False
        self.redeemed: list[tuple[str, str]] = []

    async def token(self, code: str, redirect_uri: str) -> dict[str, Any]:
        if code not in USERS:
            raise OAuthError("vexoulz-auth refused the code (400 invalid_grant)")
        self.redeemed.append((code, redirect_uri))
        user_id, login = USERS[code]
        return {"user": {"id": user_id, "login": login}, "sid": f"sid-{code}", "expiresAt": "x"}

    async def session_active(self, sid: str) -> bool:
        if self.fail:
            raise OAuthError("vexoulz-auth session check failed (503)")
        return sid not in self.signed_out

    async def moderated_channels(self, user_id: str) -> list[str]:
        if user_id in self.revoked:
            raise TokenRevoked("vexoulz-auth has no usable token (410 revoked)")
        return self.moderated.get(user_id, [])


@pytest.fixture
async def vexoulz(app_and_keys: tuple[Any, ApiKeyService]) -> dict[str, Any]:
    app = app_and_keys[0]
    policy = app.state.policy
    await policy.mutate(lambda repo: repo.ensure_channel(OWN_ID, OWN_LOGIN, Actor(None, "test")))
    await policy.mutate(lambda repo: repo.ensure_channel(PARTED_ID, "gone", Actor(None, "test")))
    await policy.mutate(lambda repo: repo.set_channel_field(PARTED_ID, "active", False, Actor(None, "test")))
    policy.owners = frozenset({"1"})
    now = [1000.0]
    http = FakeVexoulzHttp()
    app.state.twitch_signin = VexoulzSignIn(
        auth_url="https://auth.example/",
        client_id="dtp",
        redirect_uri="https://bot.example/auth/admin/callback",
        http=http,
        policy=policy,
        clock=lambda: now[0],
    )
    return {"http": http, "now": now}


async def sign_in(client: httpx.AsyncClient, code: str, next_path: str | None = None) -> httpx.Response:
    started = await client.get("/auth/admin/login", params={"next": next_path} if next_path else {})
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    return await client.get("/auth/admin/callback", params={"code": code, "state": state})


async def test_login_goes_to_vexoulz_auth_with_the_scope(client: httpx.AsyncClient, vexoulz: dict[str, Any]) -> None:
    started = await client.get("/auth/admin/login")
    location = urlsplit(started.headers["location"])
    query = parse_qs(location.query)
    assert (location.scheme, location.netloc, location.path) == ("https", "auth.example", "/authorize")
    assert query["client_id"] == ["dtp"] and query["scope"] == ["user:read:moderated_channels"]
    assert query["redirect_uri"] == ["https://bot.example/auth/admin/callback"]
    assert (await client.get("/api/v1/session")).json()["twitch_login"] is True


async def test_a_moderator_signs_in(client: httpx.AsyncClient, vexoulz: dict[str, Any]) -> None:
    done = await sign_in(client, "mod", "/admin/channels/doomtp")
    assert done.status_code == 302 and done.headers["location"] == "/admin/channels/doomtp"
    session = (await client.get("/api/v1/session")).json()
    assert (session["role"], session["user"], session["channels"]) == (
        "moderator",
        {"id": MOD_ID, "login": "mod"},
        [CHANNEL_LOGIN],
    )
    assert vexoulz["http"].redeemed == [("mod", "https://bot.example/auth/admin/callback")]


async def test_a_bot_owner_is_an_admin(client: httpx.AsyncClient, vexoulz: dict[str, Any]) -> None:
    await sign_in(client, "owner")
    assert (await client.get("/api/v1/session")).json()["role"] == "admin"


async def test_failures_reach_the_login_page(client: httpx.AsyncClient, vexoulz: dict[str, Any]) -> None:
    assert error_of(await sign_in(client, "not-a-code"))[1]["error"] == ["twitch"]
    for sent, shown in (("denied", "denied"), ("expired", "expired"), ("invalid_scope", "twitch")):
        started = await client.get("/auth/admin/login")
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        done = await client.get("/auth/admin/callback", params={"error": sent, "state": state})
        assert error_of(done)[1]["error"] == [shown]
    assert client.cookies.get(SESSION_COOKIE) is None


async def test_signing_out_everywhere_ends_the_session_at_the_next_refresh(
    client: httpx.AsyncClient, vexoulz: dict[str, Any]
) -> None:
    http, now = vexoulz["http"], vexoulz["now"]
    await sign_in(client, "owner")
    http.signed_out.add("sid-owner")
    assert (await client.get("/api/v1/session")).json()["authenticated"] is True  # not due yet

    http.fail = True  # vexoulz-auth unreachable: the session keeps what it had
    now[0] += REFRESH_S
    assert (await client.get("/api/v1/session")).json()["authenticated"] is True

    http.fail = False
    now[0] += REFRESH_S
    assert (await client.get("/api/v1/session")).json()["authenticated"] is False


async def test_channels_follow_twitch_and_a_revoked_token_ends_the_session(
    client: httpx.AsyncClient, vexoulz: dict[str, Any]
) -> None:
    http, now = vexoulz["http"], vexoulz["now"]
    await sign_in(client, "mod")
    http.moderated[MOD_ID] = [OWN_ID]
    now[0] += REFRESH_S
    assert (await client.get("/api/v1/session")).json()["channels"] == [OWN_LOGIN]

    http.revoked.add(MOD_ID)
    now[0] += REFRESH_S
    assert (await client.get("/api/v1/session")).json()["authenticated"] is False
