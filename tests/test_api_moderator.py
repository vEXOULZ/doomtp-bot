"""Moderator sessions (ADR-0017): what a moderator may use, checked on the server, and who the audit names."""
# ruff: noqa: F811  (the imported fixtures are parameters here, which ruff reads as redefinitions)

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.api.routes.data import router
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.webui.auth import SESSION_COOKIE
from tests.test_api_data import (  # noqa: F401  (fixtures)
    CHANNEL_ID,
    CHANNEL_LOGIN,
    PASSWORD,
    app_and_keys,
    client,
)

MOD_ID, OTHER_ID, OTHER_LOGIN = "300", "700", "elsewhere"

# Routes anyone may call: they show what the public pages print.
PUBLIC = {
    ("GET", "/api/v1/channels/{login}/commands"),
    ("GET", "/api/v1/custom-commands"),
    ("GET", "/api/v1/channels/{login}/publications"),
}
# Routes no moderator may call, whatever the channel.
ADMIN_ONLY = {
    ("POST", "/api/v1/channels"),
    ("DELETE", "/api/v1/channels/{login}"),
    ("PATCH", "/api/v1/channels/{login}/publications/{name}"),
    ("GET", "/api/v1/channels/{login}/runs"),
    ("GET", "/api/v1/channels/{login}/messages"),
}


def test_every_private_route_says_who_may_use_it() -> None:
    """A new route without an area would be open to every moderator in every channel. This fails first."""
    areas: dict[tuple[str, str], str] = {}
    for route in router.routes:
        assert isinstance(route, APIRoute)
        found = [d.call.area for d in route.dependant.dependencies if hasattr(d.call, "area")]
        for method in route.methods:
            key = (method, route.path)
            if key in PUBLIC:
                assert found == [], f"{key} is public but checks an area"
                continue
            assert len(found) == 1, f"{key} says nothing about who may use it"
            areas[key] = found[0]
    assert {k for k, area in areas.items() if area == "admin"} == ADMIN_ONLY


@pytest.fixture
async def mod(client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService]) -> dict[str, str]:
    """Sign `client` in as a moderator of doomtp only, as a Twitch sign-in will, and return the CSRF header."""
    app = app_and_keys[0]
    await app.state.policy.mutate(
        lambda repo: repo.ensure_channel(OTHER_ID, OTHER_LOGIN, Actor(None, "test"))
    )
    session = app.state.admin_auth.login(
        role="moderator", user_id=MOD_ID, user_login="mod", channels=frozenset({CHANNEL_LOGIN})
    )
    client.cookies.set(SESSION_COOKIE, session.token)
    return {"X-CSRF-Token": session.csrf}


async def test_the_session_says_who_is_behind_it(client: httpx.AsyncClient, mod: dict[str, str]) -> None:
    session = (await client.get("/api/v1/session")).json()
    assert session["role"] == "moderator"
    assert session["user"] == {"id": MOD_ID, "login": "mod"}
    assert session["channels"] == [CHANNEL_LOGIN]
    assert (await client.delete("/api/v1/session", headers=mod)).status_code == 204  # may still log out


async def test_a_moderator_manages_their_own_channel_and_is_audited_as_themselves(
    client: httpx.AsyncClient,
    mod: dict[str, str],
) -> None:
    channels = (await client.get("/api/v1/channels")).json()["channels"]
    assert [c["login"] for c in channels] == [CHANNEL_LOGIN]
    assert (await client.get(f"/api/v1/channels/{OTHER_LOGIN}")).status_code == 403

    own = f"/api/v1/channels/{CHANNEL_LOGIN}"
    assert (await client.put(f"{own}/modules/basic", json={"enabled": False}, headers=mod)).status_code == 200
    other = f"/api/v1/channels/{OTHER_LOGIN}/modules/basic"
    assert (await client.put(other, json={"enabled": False}, headers=mod)).status_code == 403
    assert (await client.get(f"{own}/filters")).status_code == 200
    assert (await client.get(f"{own}/triggers")).status_code == 200

    assert (await client.patch(own, json={"quiet_errors": True}, headers=mod)).json()["quiet_errors"] is True
    mixed = await client.patch(own, json={"quiet_errors": False, "log_enabled": False}, headers=mod)
    assert mixed.status_code == 403 and "log_enabled" in mixed.json()["detail"]
    after = (await client.get(own)).json()
    assert after["quiet_errors"] is True and after["log_enabled"] is True  # refused whole, nothing applied
    roles = await client.patch(own, json={"publish_min_role": "everyone"}, headers=mod)
    assert roles.status_code == 403

    entries = (await client.get("/api/v1/audit")).json()["entries"]
    assert entries and all(e["channel_id"] == CHANNEL_ID for e in entries)
    assert (entries[0]["actor_user_id"], entries[0]["via"]) == (MOD_ID, "web")
    assert (await client.get("/api/v1/audit", params={"channel": OTHER_LOGIN})).status_code == 403


async def test_a_moderator_cannot_reach_what_is_for_admins(
    client: httpx.AsyncClient, mod: dict[str, str]
) -> None:
    own = f"/api/v1/channels/{CHANNEL_LOGIN}"
    assert (await client.post("/api/v1/channels", json={"login": "friend"}, headers=mod)).status_code == 403
    assert (await client.delete(own, headers=mod)).status_code == 403
    assert (await client.get(f"{own}/runs")).status_code == 403
    assert (await client.get(f"{own}/messages", params={"q": "hi"})).status_code == 403
    assert (
        await client.patch(f"{own}/publications/x", json={"enabled": False}, headers=mod)
    ).status_code == 403
    assert (await client.get("/api/v1/keys")).status_code == 403
    assert (await client.post("/api/v1/keys", json={"name": "mine"}, headers=mod)).status_code == 403
    assert (await client.get("/admin")).status_code == 403  # the Jinja admin pages show every channel

    explain = {"text": "ping", "context": "body", "as_user": "friend"}
    assert (
        await client.post("/api/v1/explain", json={**explain, "channel": CHANNEL_LOGIN})
    ).status_code == 200
    assert (await client.post("/api/v1/explain", json={**explain, "channel": OTHER_LOGIN})).status_code == 403


async def test_a_signed_in_user_lifts_their_own_self_ignore_anywhere(
    client: httpx.AsyncClient,
    app_and_keys: tuple[Any, ApiKeyService],
    mod: dict[str, str],
) -> None:
    policy = app_and_keys[0].state.policy
    url = f"/api/v1/channels/{OTHER_LOGIN}/ignored"
    await policy.mutate(lambda repo: repo.set_ignored(OTHER_ID, MOD_ID, "mod", True, Actor(MOD_ID, "chat")))
    await policy.mutate(lambda repo: repo.set_ignored(OTHER_ID, "200", "friend", True, Actor(MOD_ID, "chat")))

    assert (await client.delete(f"{url}/200", headers=mod)).status_code == 403  # not their channel
    assert (await client.delete(f"{url}/{MOD_ID}", headers=mod)).status_code == 200  # their own `ignore me`
    assert not policy.is_ignored(OTHER_ID, MOD_ID)

    await policy.mutate(lambda repo: repo.set_ignored(OTHER_ID, MOD_ID, "mod", True, Actor(OTHER_ID, "chat")))
    refused = await client.delete(f"{url}/{MOD_ID}", headers=mod)  # the broadcaster's ignore stays
    assert refused.status_code == 403 and policy.is_ignored(OTHER_ID, MOD_ID)


async def test_only_an_admin_changes_a_bot_wide_ignore(
    client: httpx.AsyncClient,
    app_and_keys: tuple[Any, ApiKeyService],
    mod: dict[str, str],
) -> None:
    """`everywhere` writes the GLOBAL scope, which reaches channels the moderator doesn't manage."""
    policy = app_and_keys[0].state.policy
    url = f"/api/v1/channels/{CHANNEL_LOGIN}/ignored"

    refused = await client.post(url, json={"login": "friend", "everywhere": True}, headers=mod)
    assert (
        refused.status_code == 403
        and refused.json()["detail"] == "only an admin can change a bot-wide ignore"
    )
    assert not policy.is_ignored(OTHER_ID, "200")
    await policy.mutate(lambda repo: repo.set_ignored(GLOBAL, "200", "friend", True, Actor(None, "api")))
    lifted = await client.delete(f"{url}/200", params={"everywhere": True}, headers=mod)
    assert lifted.status_code == 403 and policy.is_ignored(OTHER_ID, "200")

    # Their own channel, as before; and not someone else's.
    assert (await client.post(url, json={"login": "alice"}, headers=mod)).status_code == 201
    assert policy.is_ignored(CHANNEL_ID, "400") and not policy.is_ignored(OTHER_ID, "400")
    assert (await client.delete(f"{url}/400", headers=mod)).status_code == 200
    other = f"/api/v1/channels/{OTHER_LOGIN}/ignored"
    assert (await client.post(other, json={"login": "alice"}, headers=mod)).status_code == 403
    await policy.mutate(lambda repo: repo.set_ignored(OTHER_ID, "400", "alice", True, Actor(None, "api")))
    assert (await client.delete(f"{other}/400", headers=mod)).status_code == 403


async def test_a_password_session_changes_a_bot_wide_ignore(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService]
) -> None:
    policy = app_and_keys[0].state.policy
    csrf = {
        "X-CSRF-Token": (await client.post("/api/v1/session", json={"password": PASSWORD})).json()["csrf"]
    }
    url = f"/api/v1/channels/{CHANNEL_LOGIN}/ignored"
    assert (
        await client.post(url, json={"login": "friend", "everywhere": True}, headers=csrf)
    ).status_code == 201
    assert policy.is_ignored(OTHER_ID, "200")
    lifted = await client.delete(f"{url}/200", params={"everywhere": True}, headers=csrf)
    assert lifted.status_code == 200 and not policy.is_ignored(OTHER_ID, "200")


async def test_a_moderator_cannot_reach_a_global_filter_through_their_channel(
    client: httpx.AsyncClient,
    app_and_keys: tuple[Any, ApiKeyService],
    mod: dict[str, str],
) -> None:
    """Filters, toggles and rules are written to the channel in the path, never to GLOBAL."""
    filters = app_and_keys[0].state.filters
    entry = await filters.add(channel_id=GLOBAL, pattern="everywhere", actor_user_id=None, via="api")
    url = f"/api/v1/channels/{CHANNEL_LOGIN}/filters/{entry.id}"
    assert (await client.patch(url, json={"enabled": False}, headers=mod)).status_code == 404
    assert (await client.delete(url, headers=mod)).status_code == 404
    assert [e.enabled for e in filters.entries_for(CHANNEL_ID) if e.id == entry.id] == [True]
