"""Signing in to the web admin with Twitch (ADR-0017 item 3): the flow, `channels` from Helix, the refresh."""
# ruff: noqa: F811  (the imported fixtures are parameters here, which ruff reads as redefinitions)

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.policy.repository import Actor
from doomtp_bot.twitch.auth import OAuthError
from doomtp_bot.twitch.signin import REFRESH_S, SIGNIN_SCOPES, TokenRevoked, TwitchSignIn, safe_next
from doomtp_bot.webui.auth import SESSION_COOKIE
from tests.test_api_data import (  # noqa: F401  (fixtures)
    CHANNEL_ID,
    CHANNEL_LOGIN,
    app_and_keys,
    client,
)

MOD_ID, OWN_ID, OWN_LOGIN, PARTED_ID = "300", "301", "modchannel", "302"
# The Twitch users the fake knows, by the authorization code that signs them in.
USERS = {
    "mod": (MOD_ID, "mod"),
    "own": (OWN_ID, OWN_LOGIN),
    "owner": ("1", "owner"),
    "nobody": ("9", "nobody"),
}


class FakeSignInHttp:
    def __init__(self) -> None:
        self.moderated: dict[str, list[str]] = {MOD_ID: [CHANNEL_ID, PARTED_ID, "12345"]}
        self.asked: list[str] = []
        self.expired: set[str] = set()  # access tokens Twitch now refuses
        self.fail = False

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        if code not in USERS:
            raise OAuthError("token exchange failed (400)")
        return {"access_token": f"at-{code}", "refresh_token": f"rt-{code}"}

    async def validate(self, access_token: str) -> dict[str, Any]:
        user_id, login = USERS[access_token.removeprefix("at-").split("-")[0]]
        return {"user_id": user_id, "login": login, "scopes": list(SIGNIN_SCOPES)}

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        if refresh_token == "revoked":
            raise TokenRevoked("Twitch refused the refresh token (400)")
        return {"access_token": refresh_token.replace("rt-", "at-") + "-new", "refresh_token": refresh_token}

    async def moderated_channels(self, access_token: str, user_id: str) -> list[str]:
        self.asked.append(user_id)
        if self.fail:
            raise OAuthError("Get Moderated Channels failed (503)")
        if access_token in self.expired:
            raise TokenRevoked("Twitch refused the user's token")
        return self.moderated.get(user_id, [])


@pytest.fixture
async def signin(app_and_keys: tuple[Any, ApiKeyService]) -> dict[str, Any]:
    """Twitch sign-in on the test app, with a clock the test moves. Channels: doomtp (joined), the
    moderator's own `modchannel` (joined) and one the bot parted."""
    app = app_and_keys[0]
    policy = app.state.policy
    await policy.mutate(lambda repo: repo.ensure_channel(OWN_ID, OWN_LOGIN, Actor(None, "test")))
    await policy.mutate(lambda repo: repo.ensure_channel(PARTED_ID, "gone", Actor(None, "test")))
    await policy.mutate(lambda repo: repo.set_channel_field(PARTED_ID, "active", False, Actor(None, "test")))
    policy.owners = frozenset({"1"})
    now = [1000.0]
    http = FakeSignInHttp()
    app.state.twitch_signin = TwitchSignIn(
        client_id="cid",
        redirect_uri="https://bot.example/auth/admin/callback",
        http=http,
        policy=policy,
        clock=lambda: now[0],
    )
    return {"http": http, "now": now, "signin": app.state.twitch_signin}


async def sign_in(client: httpx.AsyncClient, code: str, next_path: str | None = None) -> httpx.Response:
    """Follow the flow as a browser would: to Twitch, which sends the user back with `code`."""
    started = await client.get("/auth/admin/login", params={"next": next_path} if next_path else {})
    assert started.status_code == 302
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    return await client.get("/auth/admin/callback", params={"code": code, "state": state})


def error_of(response: httpx.Response) -> tuple[str, dict[str, list[str]]]:
    location = urlsplit(response.headers["location"])
    return location.path, parse_qs(location.query)


async def test_sign_in_asks_twitch_for_moderated_channels_only(
    client: httpx.AsyncClient, signin: dict[str, Any]
) -> None:
    started = await client.get("/auth/admin/login")
    location = urlsplit(started.headers["location"])
    query = parse_qs(location.query)
    assert (location.netloc, location.path) == ("id.twitch.tv", "/oauth2/authorize")
    assert query["scope"] == ["user:read:moderated_channels"]
    assert query["redirect_uri"] == ["https://bot.example/auth/admin/callback"]
    assert (
        "doomtp_signin" in started.headers["set-cookie"]
        and "Path=/auth/admin" in started.headers["set-cookie"]
    )
    assert (await client.get("/api/v1/session")).json()["twitch_login"] is True


async def test_a_moderator_signs_in_and_lands_where_they_started(
    client: httpx.AsyncClient, signin: dict[str, Any]
) -> None:
    done = await sign_in(client, "mod", "/admin/channels/doomtp?tab=filters")
    assert done.status_code == 302 and done.headers["location"] == "/admin/channels/doomtp?tab=filters"
    session = (await client.get("/api/v1/session")).json()
    assert (session["role"], session["user"]) == ("moderator", {"id": MOD_ID, "login": "mod"})
    assert session["channels"] == [CHANNEL_LOGIN]  # Helix's list, less the parted and unknown channels
    assert (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}")).status_code == 200
    assert (await client.get(f"/api/v1/channels/{OWN_LOGIN}")).status_code == 403


async def test_a_broadcaster_manages_their_own_channel(
    client: httpx.AsyncClient, signin: dict[str, Any]
) -> None:
    await sign_in(client, "own")
    assert (await client.get("/api/v1/session")).json()["channels"] == [OWN_LOGIN]


async def test_a_bot_owner_is_an_admin_without_asking_helix(
    client: httpx.AsyncClient, signin: dict[str, Any]
) -> None:
    assert (await sign_in(client, "owner")).headers["location"] == "/admin"
    session = (await client.get("/api/v1/session")).json()
    assert (session["role"], session["channels"]) == ("admin", None)
    assert signin["http"].asked == []
    assert (await client.get("/api/v1/keys")).status_code == 200


async def test_a_global_bot_admin_is_an_admin(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], signin: dict[str, Any]
) -> None:
    await app_and_keys[0].state.policy.mutate(
        lambda repo: repo.set_global_admin(MOD_ID, "mod", True, Actor("1", "chat"))
    )
    await sign_in(client, "mod")
    assert (await client.get("/api/v1/session")).json()["role"] == "admin"


async def test_someone_who_manages_no_channel_gets_no_session(
    client: httpx.AsyncClient, signin: dict[str, Any]
) -> None:
    done = await sign_in(client, "nobody", "/admin/audit")
    assert error_of(done) == ("/admin/login", {"error": ["no_channels"], "next": ["/admin/audit"]})
    assert client.cookies.get(SESSION_COOKIE) is None
    assert (await client.get("/api/v1/session")).json()["authenticated"] is False


async def test_failures_go_back_to_the_login_page_with_a_reason(
    client: httpx.AsyncClient, signin: dict[str, Any]
) -> None:
    started = await client.get("/auth/admin/login")
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    denied = await client.get("/auth/admin/callback", params={"error": "access_denied", "state": state})
    assert error_of(denied) == ("/admin/login", {"error": ["denied"]})
    again = await client.get("/auth/admin/callback", params={"code": "mod", "state": state})
    assert error_of(again)[1]["error"] == ["expired"]  # a state is good once

    await client.get("/auth/admin/login")
    assert error_of(await client.get("/auth/admin/callback", params={"code": "mod", "state": "made-up"}))[
        1
    ] == {"error": ["expired"]}
    bad_code = await sign_in(client, "not-a-code")
    assert error_of(bad_code)[1]["error"] == ["twitch"]


async def test_a_callback_from_another_browser_is_refused(
    client: httpx.AsyncClient, signin: dict[str, Any]
) -> None:
    """Otherwise a page could sign a visitor in as the page's author, with a code the author obtained."""
    started = await client.get("/auth/admin/login")
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.cookies.delete("doomtp_signin", path="/auth/admin")
    done = await client.get("/auth/admin/callback", params={"code": "mod", "state": state})
    assert error_of(done)[1]["error"] == ["expired"] and client.cookies.get(SESSION_COOKIE) is None


@pytest.mark.parametrize(
    ("given", "lands"),
    [
        ("/admin/channels/doomtp?tab=filters#top", "/admin/channels/doomtp?tab=filters#top"),
        ("https://evil.example/admin", "/admin"),
        ("//evil.example/admin", "/admin"),
        ("/\\evil.example", "/admin"),
        ("admin", "/admin"),
        ("/admin\r\nSet-Cookie: x=1", "/admin"),
        (None, "/admin"),
    ],
)
def test_next_is_only_ever_a_path_on_this_site(given: str | None, lands: str) -> None:
    assert safe_next(given) == lands


async def test_a_full_url_as_next_lands_on_the_admin_home(
    client: httpx.AsyncClient, signin: dict[str, Any]
) -> None:
    assert (await sign_in(client, "mod", "https://evil.example/")).headers["location"] == "/admin"


async def test_channels_are_refreshed_every_few_minutes(
    client: httpx.AsyncClient, signin: dict[str, Any]
) -> None:
    http, now = signin["http"], signin["now"]
    await sign_in(client, "mod")
    http.moderated[MOD_ID] = [OWN_ID]
    assert (await client.get("/api/v1/session")).json()["channels"] == [CHANNEL_LOGIN]  # not yet due

    now[0] += REFRESH_S
    assert (await client.get("/api/v1/session")).json()["channels"] == [OWN_LOGIN]
    assert (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}")).status_code == 403  # dropped at once

    http.fail = True  # Twitch having a bad minute keeps what the session had
    now[0] += REFRESH_S
    assert (await client.get("/api/v1/session")).json()["channels"] == [OWN_LOGIN]

    http.fail, http.moderated[MOD_ID] = False, []
    now[0] += REFRESH_S
    assert (await client.get(f"/api/v1/channels/{OWN_LOGIN}")).status_code == 401  # no channels, no session
    assert (await client.get("/api/v1/session")).json()["authenticated"] is False


async def test_an_expired_token_is_refreshed_and_a_revoked_one_ends_the_session(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], signin: dict[str, Any]
) -> None:
    http, now = signin["http"], signin["now"]
    await sign_in(client, "mod")
    grant = app_and_keys[0].state.admin_auth.session(client.cookies.get(SESSION_COOKIE)).grant
    http.expired.add("at-mod")
    now[0] += REFRESH_S
    assert (await client.get("/api/v1/session")).json()["channels"] == [CHANNEL_LOGIN]  # with a new token
    assert grant.access_token == "at-mod-new"

    http.expired.add("at-mod-new")
    grant.refresh_token = "revoked"  # the user disconnected the app on Twitch
    now[0] += REFRESH_S
    assert (await client.get("/api/v1/session")).json()["authenticated"] is False


async def test_without_twitch_credentials_the_site_offers_no_button(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/session")).json()["twitch_login"] is False
    started = await client.get("/auth/admin/login", params={"next": "/admin/audit"})
    assert error_of(started) == ("/admin/login", {"error": ["not_configured"], "next": ["/admin/audit"]})
