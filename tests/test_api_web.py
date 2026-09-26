"""The JSON a web UI served elsewhere needs (ADR-0016): session login, API keys, and the public reads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from doomtp_bot.api.app import create_app
from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.core.health import ComponentHealth, HealthRegistry, Status
from doomtp_bot.customcmds.packs import PackService
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.filters.service import FilterService
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.roles import BUILTIN_RANKS, GLOBAL
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.runtime.explain import ReportStore
from doomtp_bot.storage.db import Databases
from doomtp_bot.webui.auth import LoginLimiter
from doomtp_bot.webui.pages import GRAMMAR_RULES
from tests.fakes import policy_with_channels

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
PASSWORD = "correct horse battery staple"


async def _ok_check() -> ComponentHealth:
    return ComponentHealth(Status.OK, {})


@pytest.fixture
async def app(dbs: Databases) -> AsyncIterator[Any]:
    policy = await policy_with_channels(dbs.bot, (CHANNEL_ID, CHANNEL_LOGIN), joined=True)
    filters = FilterService(dbs.bot)
    await filters.reload()
    customcmds = CustomCommandService(dbs.bot, filters=filters)
    health = HealthRegistry()
    health.register("databases", _ok_check)
    yield create_app(
        health,
        None,
        runtime=Runtime(builtin_registry(), policy=policy, services={"policy": policy}),
        policy=policy,
        services={
            "customcmds": customcmds,
            "packs": PackService(dbs.bot, customcmds),
            "api_keys": ApiKeyService(dbs.bot),
            "explain_reports": ReportStore("https://bot.example"),
        },
        admin_password=PASSWORD,
    )


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def _login(client: httpx.AsyncClient) -> str:
    """Log in and return the CSRF token every change must carry."""
    response = await client.post("/api/v1/session", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["csrf"])


# ── session ────────────────────────────────────────────────────────────────
async def test_logging_in_and_out(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/session")).json() == {
        "authenticated": False,
        "csrf": None,
        "expires_at": None,
        "admin_enabled": True,
    }
    assert (await client.post("/api/v1/session", json={"password": "nope"})).status_code == 401

    response = await client.post("/api/v1/session", json={"password": PASSWORD})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie  # plain http here
    session = (await client.get("/api/v1/session")).json()
    assert session["authenticated"] and session["csrf"] == response.json()["csrf"] and session["expires_at"]

    # The same session works on the data API, and it is the one the admin pages use.
    assert (await client.get("/api/v1/channels")).status_code == 200
    assert (await client.get("/admin")).status_code == 200

    assert (await client.delete("/api/v1/session")).status_code == 403  # no CSRF token: a cross-site page
    assert (
        await client.delete("/api/v1/session", headers={"X-CSRF-Token": session["csrf"]})
    ).status_code == 204
    assert (await client.get("/api/v1/session")).json()["authenticated"] is False
    assert (await client.get("/api/v1/channels")).status_code == 401


async def test_the_cookie_is_secure_behind_https(app: Any) -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as http:
        response = await http.post("/api/v1/session", json={"password": PASSWORD})
    assert "secure" in response.headers["set-cookie"].lower()


async def test_logins_are_rate_limited_per_address(client: httpx.AsyncClient, app: Any) -> None:
    app.state.login_limiter = LoginLimiter(attempts=2)
    for _ in range(2):
        assert (await client.post("/api/v1/session", json={"password": "nope"})).status_code == 401
    refused = await client.post("/api/v1/session", json={"password": PASSWORD})
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) > 0
    # The admin page's form shares the limit, so it is no way around it.
    form = await client.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
    assert "too+many" in form.headers["location"]


def test_the_limiter_forgets_old_failures_and_resets_on_success() -> None:
    now = [0.0]
    limiter = LoginLimiter(attempts=2, window_s=60, clock=lambda: now[0])
    limiter.failed("a")
    limiter.failed("a")
    assert limiter.retry_after("a") == 60 and limiter.retry_after("b") is None
    now[0] = 59.5
    assert limiter.retry_after("a") == 1
    now[0] = 60
    assert limiter.retry_after("a") is None
    limiter.failed("a")
    limiter.reset("a")
    assert limiter.retry_after("a") is None


async def test_login_is_off_without_a_password() -> None:
    app = create_app(HealthRegistry(), None, admin_password=None)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get("/api/v1/session")).json()["admin_enabled"] is False
        assert (await http.post("/api/v1/session", json={"password": "x"})).status_code == 404


# ── API keys ───────────────────────────────────────────────────────────────
async def test_keys_are_managed_with_a_session_and_shown_once(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/keys")).status_code == 401
    csrf = await _login(client)
    assert (await client.post("/api/v1/keys", json={"name": "grafana"})).status_code == 403  # no CSRF

    created = await client.post(
        "/api/v1/keys", json={"name": "grafana", "scopes": ["read"]}, headers={"X-CSRF-Token": csrf}
    )
    assert created.status_code == 201
    key = created.json()
    assert key["secret"].startswith("dtb_") and key["scopes"] == ["read"] and key["last_used_at"] is None

    listed = (await client.get("/api/v1/keys")).json()["keys"]
    assert [k["name"] for k in listed] == ["grafana"] and "secret" not in listed[0]

    bad = await client.post(
        "/api/v1/keys", json={"name": "x", "scopes": ["admin"]}, headers={"X-CSRF-Token": csrf}
    )
    assert bad.status_code == 400

    revoked = await client.delete(f"/api/v1/keys/{key['id']}", headers={"X-CSRF-Token": csrf})
    assert revoked.json() == {"id": key["id"], "revoked": True}
    assert (
        await client.delete(f"/api/v1/keys/{key['id']}", headers={"X-CSRF-Token": csrf})
    ).status_code == 404
    assert (await client.get("/api/v1/keys")).json()["keys"] == []


async def test_a_key_cannot_manage_keys(client: httpx.AsyncClient, app: Any) -> None:
    _, secret = await app.state.api_keys.create(name="script", scopes=("read", "write"))
    headers = {"Authorization": f"Bearer {secret}"}
    assert (await client.get("/api/v1/keys", headers=headers)).status_code == 401
    assert (await client.post("/api/v1/keys", json={"name": "more"}, headers=headers)).status_code == 401


# ── admin reads ────────────────────────────────────────────────────────────
async def test_a_channels_modules_and_ignored_users(client: httpx.AsyncClient, app: Any) -> None:
    assert (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/modules")).status_code == 401
    await _login(client)
    modules = (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/modules")).json()["modules"]
    names = [m["module"] for m in modules]
    assert names == sorted(names) and all(m["enabled"] for m in modules)
    assert any(not m["toggleable"] for m in modules)  # core can't be turned off
    assert {m["kind"] for m in modules} == {"builtin"}  # nothing published here yet

    policy = app.state.policy
    actor = Actor(None, "test")
    await policy.mutate(lambda repo: repo.set_ignored(CHANNEL_ID, "555", "pest", True, actor))
    await policy.mutate(lambda repo: repo.set_ignored(GLOBAL, "666", "spammer", True, actor))
    found = (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/ignored")).json()
    assert [(e["user_id"], e["login"]) for e in found["ignored"]] == [("555", "pest")]
    assert [(e["user_id"], e["login"]) for e in found["ignored_everywhere"]] == [("666", "spammer")]


# ── public reads ───────────────────────────────────────────────────────────
async def test_the_site_lists_joined_channels_without_signing_in(client: httpx.AsyncClient) -> None:
    site = (await client.get("/api/v1/site")).json()
    assert site["admin_enabled"] is True and site["version"] and site["syntax_version"]
    assert [c["login"] for c in site["channels"]] == [CHANNEL_LOGIN]
    assert set(site["channels"][0]) == {"login", "prefix", "tier"}  # nothing the home page doesn't show


async def test_roles_and_grammar_are_public(client: httpx.AsyncClient) -> None:
    roles = (await client.get("/api/v1/roles")).json()
    assert [r["name"] for r in roles["roles"]][0] == "everyone"
    assert {r["name"]: r["rank"] for r in roles["roles"]} == BUILTIN_RANKS
    grammar = (await client.get("/api/v1/grammar")).json()
    assert grammar["rules"] == GRAMMAR_RULES and grammar["text"]


async def test_an_explain_link_serves_its_report(client: httpx.AsyncClient, app: Any) -> None:
    token = app.state.explain_reports.keep({"expression": "!role list", "invocations": []})
    assert (await client.get(f"/api/v1/explain/{token}")).json()["expression"] == "!role list"
    assert (await client.get("/api/v1/explain/not-a-token")).status_code == 404


async def test_published_packs_are_public(client: httpx.AsyncClient, app: Any) -> None:
    customcmds, packs = app.state.customcmds, app.state.packs
    dice = await customcmds.create(
        owner_user_id="400",
        owner_login="alice",
        name="dice",
        body="random 1 6",
        channel_id=GLOBAL,
        prefix="!",
    )
    hype = await customcmds.create(
        owner_user_id="400",
        owner_login="alice",
        name="hype",
        body="echo hyped",
        channel_id=CHANNEL_ID,
        prefix="!",
    )
    games = await packs.create(owner_user_id="400", name="games", summary="little games")
    await packs.add_member(games, dice)
    await packs.publish(channel_id=GLOBAL, pack=games, published_by="1")
    local = await packs.create(owner_user_id="400", name="local")
    await packs.add_member(local, hype)
    await packs.publish(channel_id=CHANNEL_ID, pack=local, published_by="1")

    everywhere = (await client.get("/api/v1/packs")).json()["packs"]
    assert [(p["name"], p["scope"], [c["name"] for c in p["commands"]]) for p in everywhere] == [
        ("games", "global", ["dice"])
    ]
    here = (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/packs")).json()["packs"]
    assert sorted((p["name"], p["scope"]) for p in here) == [("games", "global"), ("local", "channel")]
    assert (await client.get("/api/v1/channels/nobody/packs")).status_code == 404


async def test_a_channel_page_summary_is_public(client: httpx.AsyncClient) -> None:
    channel = (await client.get(f"/api/v1/site/channels/{CHANNEL_LOGIN}")).json()
    assert channel == {"login": CHANNEL_LOGIN, "prefix": channel["prefix"], "tier": channel["tier"],
                       "status": "joined", "active": True}  # fmt: skip
    assert (await client.get("/api/v1/site/channels/nobody")).status_code == 404


async def test_command_listings_carry_what_the_command_table_shows(
    client: httpx.AsyncClient, app: Any
) -> None:
    builtins = {c["name"]: c for c in (await client.get("/api/v1/commands")).json()["commands"]}
    assert all({"toggleable", "fixed_policy"} <= set(c) for c in builtins.values())
    assert all("choices" in p for c in builtins.values() for p in c["params"])

    customcmds, packs = app.state.customcmds, app.state.packs
    hug = await customcmds.create(
        owner_user_id="400",
        owner_login="alice",
        name="hug",
        body="echo hugs {arg.1}",
        channel_id=GLOBAL,
        prefix="!",
    )
    await customcmds.set_params(hug, [{"position": "1", "name": "target", "type": "str"}])
    games = await packs.create(owner_user_id="400", name="games")
    await packs.add_member(games, hug)
    await packs.publish(channel_id=GLOBAL, pack=games, published_by="1")
    (listed,) = (await client.get("/api/v1/packs")).json()["packs"][0]["commands"]
    assert [(p["position"], p["name"]) for p in listed["params"]] == [("1", "target")]
