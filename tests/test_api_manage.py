"""The routes for what chat manages (ADR-0026): a user's own commands and packs, publishing and write grants,
roles, replies, readouts, channel variables, and the bot-wide settings. Each goes through the service chat
uses, at the rank chat asks for."""
# ruff: noqa: F811  (the imported fixtures are parameters here, which ruff reads as redefinitions)

from __future__ import annotations

from typing import Any

import httpx
import pytest

from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.api.sessions import SESSION_COOKIE
from doomtp_bot.policy.repository import Actor
from tests.test_api_data import (  # noqa: F401  (fixtures)
    CHANNEL_ID,
    CHANNEL_LOGIN,
    app_and_keys,
    auth,
    client,
    write_key,
)

OWN = f"/api/v1/channels/{CHANNEL_LOGIN}"


def sign_in(client: httpx.AsyncClient, app: Any, **session: Any) -> dict[str, str]:
    """Sign `client` in as a Twitch sign-in would, and return the CSRF header."""
    made = app.state.admin_auth.login(**session)
    client.cookies.set(SESSION_COOKIE, made.token)
    return {"X-CSRF-Token": made.csrf}


def as_user(client: httpx.AsyncClient, app: Any) -> dict[str, str]:
    """friend: moderates nothing the bot is in."""
    return sign_in(client, app, role="user", user_id="200", user_login="friend")


def as_mod(client: httpx.AsyncClient, app: Any) -> dict[str, str]:
    return sign_in(client, app, role="moderator", user_id="300", user_login="mod", channels=frozenset({CHANNEL_LOGIN}))


def as_broadcaster(client: httpx.AsyncClient, app: Any) -> dict[str, str]:
    return sign_in(
        client,
        app,
        role="moderator",
        user_id=CHANNEL_ID,
        user_login=CHANNEL_LOGIN,
        channels=frozenset({CHANNEL_LOGIN}),
    )


def as_admin(client: httpx.AsyncClient, app: Any, user_id: str = "400", login: str = "alice") -> dict[str, str]:
    return sign_in(client, app, role="admin", user_id=user_id, user_login=login)


@pytest.fixture
def app(app_and_keys: tuple[Any, ApiKeyService]) -> Any:
    return app_and_keys[0]


async def _command(client: httpx.AsyncClient, csrf: dict[str, str], name: str, body: str) -> None:
    made = await client.post(
        "/api/v1/me/custom-commands",
        json={"name": name, "body": body, "channel": CHANNEL_LOGIN},
        headers=csrf,
    )
    assert made.status_code == 201, made.text


# ── my commands ─────────────────────────────────────────────────────────────
async def test_someone_who_manages_nothing_writes_their_own_commands(client: httpx.AsyncClient, app: Any) -> None:
    csrf = as_user(client, app)
    made = await client.post(
        "/api/v1/me/custom-commands",
        json={"name": "hello", "body": "echo hi", "summary": "says hi", "channel": CHANNEL_LOGIN},
        headers=csrf,
    )
    assert made.status_code == 201 and made.json()["summary"] == "says hi"
    edited = await client.patch("/api/v1/me/custom-commands/hello", json={"body": "echo hey"}, headers=csrf)
    assert edited.status_code == 200 and edited.json()["body"] == "echo hey"

    mine = (await client.get("/api/v1/me/custom-commands")).json()
    assert [c["name"] for c in mine["commands"]] == ["hello"] and mine["quota"] > 0
    versions = (await client.get("/api/v1/me/custom-commands/hello/versions")).json()["versions"]
    assert len(versions) == 2
    oldest = min(v["version"] for v in versions)
    reverted = await client.post("/api/v1/me/custom-commands/hello/revert", json={"version": oldest}, headers=csrf)
    assert reverted.status_code == 200 and reverted.json()["body"] == "echo hi"

    bad = await client.patch("/api/v1/me/custom-commands/hello", json={"body": "echo {"}, headers=csrf)
    assert bad.status_code == 400  # refused as `cc edit` refuses it
    assert (await client.get("/api/v1/me/custom-commands/nope/versions")).status_code == 404

    gone = await client.delete("/api/v1/me/custom-commands/hello", headers=csrf)
    assert gone.status_code == 200 and gone.json()["removed"] is True
    assert (await client.get("/api/v1/me/custom-commands")).json()["commands"] == []

    audit = (await client.get("/api/v1/audit")).json()["entries"]
    assert audit and all(e["actor_user_id"] == "200" for e in audit)


async def test_a_key_has_no_commands_of_its_own(client: httpx.AsyncClient, write_key: str) -> None:
    refused = await client.get("/api/v1/me/custom-commands", headers=auth(write_key))
    assert refused.status_code == 403


async def test_the_channel_decides_who_may_create_there(client: httpx.AsyncClient, app: Any) -> None:
    await app.state.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "create_min_role", "moderator", Actor(None, "test"))
    )
    csrf = as_user(client, app)
    refused = await client.post(
        "/api/v1/me/custom-commands",
        json={"name": "x", "body": "echo x", "channel": CHANNEL_LOGIN},
        headers=csrf,
    )
    assert refused.status_code == 403 and "moderator" in refused.json()["detail"]
    await _command(client, as_mod(client, app), "x", "echo x")


# ── packs, publishing, grants ───────────────────────────────────────────────
async def test_packs_are_built_shared_and_published(client: httpx.AsyncClient, app: Any) -> None:
    csrf = as_user(client, app)
    await _command(client, csrf, "wave", "echo o/")
    assert (await client.post("/api/v1/me/packs", json={"name": "kit"}, headers=csrf)).status_code == 201
    added = await client.put("/api/v1/me/packs/kit/commands/wave", json={}, headers=csrf)
    assert added.status_code == 200 and [c["name"] for c in added.json()["commands"]] == ["wave"]
    shared = await client.patch("/api/v1/me/packs/kit", json={"shareable": True}, headers=csrf)
    assert shared.status_code == 200
    assert [p["name"] for p in (await client.get("/api/v1/me/packs")).json()["packs"]] == ["kit"]

    # Publishing here is for moderators, as `publish_min_role` says; friend isn't one.
    refused = await client.post(f"{OWN}/packs", json={"pack": "kit"}, headers=csrf)
    assert refused.status_code == 403
    csrf = as_mod(client, app)
    published = await client.post(f"{OWN}/packs", json={"pack": "kit", "owner": "friend"}, headers=csrf)
    assert published.status_code == 201 and published.json()["commands"] == ["wave"]
    public = (await client.get(f"{OWN}/packs")).json()["packs"]
    assert [(p["name"], p["owner"], p["scope"]) for p in public] == [("kit", "friend", "channel")]
    assert (await client.delete(f"{OWN}/packs/kit?owner=friend", headers=csrf)).status_code == 200
    assert (await client.get(f"{OWN}/packs")).json()["packs"] == []


async def test_a_published_command_waits_for_its_write_grant(client: httpx.AsyncClient, app: Any) -> None:
    csrf = as_mod(client, app)
    await _command(client, csrf, "deaths", "echo 1 -> channel.deaths")
    published = await client.post(f"{OWN}/publications", json={"command": "deaths"}, headers=csrf)
    assert published.status_code == 201
    assert published.json()["needs_grants"] == {"deaths": ["channel.deaths"]}

    grants = (await client.get(f"{OWN}/grants")).json()["grants"]
    assert [(g["name"], g["writes"], g["granted"]) for g in grants] == [("deaths", ["channel.deaths"], [])]
    assert (await client.put(f"{OWN}/grants/deaths/channel.deaths", headers=csrf)).status_code == 200
    grants = (await client.get(f"{OWN}/grants")).json()["grants"]
    assert grants[0]["granted"] == ["channel.deaths"]
    assert (await client.delete(f"{OWN}/grants/deaths/channel.deaths", headers=csrf)).status_code == 200

    public = (await client.get(f"{OWN}/publications")).json()
    assert "deaths" in str(public)
    assert (await client.delete(f"{OWN}/publications/deaths", headers=csrf)).status_code == 200
    assert (await client.delete(f"{OWN}/publications/deaths", headers=csrf)).status_code == 404

    as_user(client, app)
    assert (await client.get(f"{OWN}/grants")).status_code == 403


# ── roles, replies, readouts, variables ─────────────────────────────────────
async def test_roles_follow_the_rank_rules_of_role_in_chat(client: httpx.AsyncClient, app: Any) -> None:
    csrf = as_mod(client, app)
    made = await client.post(f"{OWN}/roles", json={"name": "helper", "rank": 50}, headers=csrf)
    assert made.status_code == 201
    too_high = await client.post(f"{OWN}/roles", json={"name": "boss", "rank": 95}, headers=csrf)
    assert too_high.status_code == 422 or too_high.status_code == 403
    given = await client.put(f"{OWN}/roles/helper/members/friend", json={"duration_s": 3600}, headers=csrf)
    assert given.status_code == 200 and given.json()["expires_at"] is not None
    listed = (await client.get(f"{OWN}/roles")).json()
    helper = next(r for r in listed["roles"] if r["name"] == "helper")
    assert helper["manageable"] and [m["login"] for m in helper["members"]] == ["friend"]
    assert listed["your_rank"] == 80
    assert (await client.delete(f"{OWN}/roles/helper/members/friend", headers=csrf)).status_code == 200
    assert (await client.delete(f"{OWN}/roles/helper/members/friend", headers=csrf)).status_code == 404
    assert (await client.delete(f"{OWN}/roles/helper", headers=csrf)).status_code == 200

    as_user(client, app)
    assert (await client.get(f"{OWN}/roles")).status_code == 403


async def test_replies_and_readouts_are_checked_like_chat(client: httpx.AsyncClient, app: Any) -> None:
    csrf = as_mod(client, app)
    set_ = await client.put(f"{OWN}/callbacks/on_cooldown/channel", json={"expr": "echo slow down"}, headers=csrf)
    assert set_.status_code == 200
    assert (await client.get(f"{OWN}/callbacks")).json()["callbacks"] == [
        {"scope": "channel", "kind": "on_cooldown", "expr": "echo slow down"}
    ]
    bad_scope = await client.put(f"{OWN}/callbacks/on_denied/nowhere", json={"expr": "echo no"}, headers=csrf)
    assert bad_scope.status_code == 400
    assert (await client.delete(f"{OWN}/callbacks/on_cooldown/channel", headers=csrf)).status_code == 200
    assert (await client.delete(f"{OWN}/callbacks/on_cooldown/channel", headers=csrf)).status_code == 404

    echo = await client.put(f"{OWN}/customecho/uptime", json={"text": "live for {$channel.uptime:human}"}, headers=csrf)
    assert echo.status_code == 200, echo.text
    assert (await client.get(f"{OWN}/customecho")).json()["customecho"] == [
        {"command": "uptime", "template": "live for {$channel.uptime:human}"}
    ]
    assert (await client.delete(f"{OWN}/customecho/uptime", headers=csrf)).status_code == 200
    assert (await client.delete(f"{OWN}/customecho/uptime", headers=csrf)).status_code == 404


async def test_channel_variables_are_written_by_the_role_the_channel_names(client: httpx.AsyncClient, app: Any) -> None:
    csrf = as_user(client, app)
    assert (await client.put(f"{OWN}/variables/goal", json={"value": 5}, headers=csrf)).status_code == 403
    csrf = as_mod(client, app)
    assert (await client.put(f"{OWN}/variables/goal", json={"value": 5}, headers=csrf)).status_code == 200
    assert (await client.put(f"{OWN}/variables/goal", json={"value": None}, headers=csrf)).status_code == 400
    assert (await client.delete(f"{OWN}/variables/goal", headers=csrf)).status_code == 200
    assert (await client.delete(f"{OWN}/variables/goal", headers=csrf)).status_code == 404


async def test_filter_and_listener_tests_run_nothing(client: httpx.AsyncClient, app: Any) -> None:
    csrf = as_mod(client, app)
    added = await client.post(
        f"{OWN}/filters", json={"pattern": "badword", "kind": "word", "action": "block"}, headers=csrf
    )
    assert added.status_code == 201, added.text
    tested = await client.post(f"{OWN}/filters/test", json={"text": "a badword here"}, headers=csrf)
    assert tested.status_code == 200 and tested.json()["blocked"] is True
    clean = await client.post(f"{OWN}/filters/test", json={"text": "all fine"}, headers=csrf)
    assert clean.json()["blocked"] is False
    listeners = await client.post(f"{OWN}/triggers/test", json={"text": "hello"}, headers=csrf)
    assert listeners.status_code == 200 and listeners.json()["matches"] == []


# ── the bot-wide settings ───────────────────────────────────────────────────
async def test_only_a_bot_owner_adds_bot_admins(client: httpx.AsyncClient, app: Any) -> None:
    csrf = as_admin(client, app)
    listed = (await client.get("/api/v1/admins")).json()
    assert listed["you_manage"] is False
    assert (await client.post("/api/v1/admins", json={"login": "friend"}, headers=csrf)).status_code == 403

    app.state.policy.owners = frozenset({"400"})
    added = await client.post("/api/v1/admins", json={"login": "friend"}, headers=csrf)
    assert added.status_code == 201 and added.json()["user_id"] == "200"
    listed = (await client.get("/api/v1/admins")).json()
    assert [a["login"] for a in listed["admins"]] == ["friend"] and listed["you_manage"] is True
    assert (await client.delete("/api/v1/admins/400", headers=csrf)).status_code == 400  # an owner
    assert (await client.delete("/api/v1/admins/200", headers=csrf)).status_code == 200
    assert (await client.delete("/api/v1/admins/200", headers=csrf)).status_code == 404


async def test_bot_wide_toggles_filters_and_publications(client: httpx.AsyncClient, app: Any) -> None:
    csrf = as_admin(client, app)
    modules = (await client.get("/api/v1/global/modules")).json()["modules"]
    assert any(m["module"] == "basic" for m in modules)
    off = await client.put("/api/v1/global/modules/basic", json={"enabled": False}, headers=csrf)
    assert off.status_code == 200
    modules = (await client.get("/api/v1/global/modules")).json()["modules"]
    assert next(m for m in modules if m["module"] == "basic")["enabled"] is False
    assert (await client.delete("/api/v1/global/modules/basic", headers=csrf)).status_code == 200

    commands = (await client.get("/api/v1/global/commands")).json()["commands"]
    name = next(c["name"] for c in commands if c["toggleable"] and not c["fixed_policy"])
    rule = await client.patch(f"/api/v1/global/commands/{name}", json={"enabled": False}, headers=csrf)
    assert rule.status_code == 200 and rule.json()["enabled"] is False, rule.text
    reset = await client.delete(f"/api/v1/global/commands/{name}", headers=csrf)
    assert reset.status_code == 200 and reset.json()["enabled"] is True

    added = await client.post(
        "/api/v1/global/filters", json={"pattern": "spam", "kind": "word", "action": "block"}, headers=csrf
    )
    assert added.status_code == 201
    entry_id = added.json()["id"]
    assert [f["id"] for f in (await client.get("/api/v1/global/filters")).json()["filters"]] == [entry_id]
    assert (await client.delete(f"/api/v1/global/filters/{entry_id}", headers=csrf)).status_code == 200
    assert (await client.get("/api/v1/ignored")).json()["ignored"] == []

    await _command(client, csrf, "hi", "echo hi")
    assert (await client.post("/api/v1/global/publications", json={"command": "hi"}, headers=csrf)).status_code == 201
    assert "hi" in str((await client.get("/api/v1/custom-commands")).json())
    assert (await client.delete("/api/v1/global/publications/hi", headers=csrf)).status_code == 200

    assert (await client.post(f"{OWN}/capabilities/probe", headers=csrf)).json()["capabilities"] == ["moderate"]

    as_broadcaster(client, app)
    assert (await client.get("/api/v1/global/modules")).status_code == 403
    assert (await client.get("/api/v1/admins")).status_code == 403


async def test_params_and_links_go_through_cc_param_and_cc_link(client: httpx.AsyncClient, app: Any) -> None:
    csrf = as_mod(client, app)
    await _command(client, csrf, "greet", "echo hi {arg.1}")
    declared = await client.put(
        "/api/v1/me/custom-commands/greet/params/1",
        json={"name": "who", "type": "user", "description": "who to greet"},
        headers=csrf,
    )
    assert declared.status_code == 200, declared.text
    assert [p["name"] for p in declared.json()["params"]] == ["who"]
    assert (await client.delete("/api/v1/me/custom-commands/greet/params/1", headers=csrf)).status_code == 200
    assert (await client.delete("/api/v1/me/custom-commands/greet/params/1", headers=csrf)).status_code == 404
    await client.patch("/api/v1/me/custom-commands/greet", json={"shareable": True}, headers=csrf)
    own = await client.put("/api/v1/me/links/hey", json={"command": "greet", "owner": "mod"}, headers=csrf)
    assert own.status_code == 400

    csrf = as_user(client, app)
    linked = await client.put("/api/v1/me/links/hey", json={"command": "greet", "owner": "mod"}, headers=csrf)
    assert linked.status_code == 200 and linked.json()["alias"] == "hey"
    assert [c["alias"] for c in (await client.get("/api/v1/me/custom-commands")).json()["linked"]] == ["hey"]
    assert (await client.delete("/api/v1/me/links/hey", headers=csrf)).status_code == 200
    assert (await client.delete("/api/v1/me/links/hey", headers=csrf)).status_code == 404
