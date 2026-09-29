"""Signed-in sessions (ADR-0017, ADR-0026): what a moderator, a broadcaster and a plain user may use, checked
on the server at the rank chat gives them, and who the audit names."""
# ruff: noqa: F811  (the imported fixtures are parameters here, which ruff reads as redefinitions)

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.api.routes.data import router
from doomtp_bot.api.sessions import SESSION_COOKIE, ReadLimiter
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.roles import BROADCASTER_RANK, GLOBAL, MODERATOR_RANK
from scripts.dev_api import add_dev_login, parse_moderators
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
# Routes anyone may call while the channel's log is public, and its moderators otherwise (ADR-0026).
PUBLIC_WHEN_OPEN = {
    ("GET", "/api/v1/channels/{login}/messages"),
    ("GET", "/api/v1/channels/{login}/log"),
    ("GET", "/api/v1/channels/{login}/log/coverage"),
}
# Routes for anyone signed in, about themselves.
PERSONAL = {("POST", "/api/v1/me/channel")}
# Channel routes that need the broadcaster's rank, as their chat commands do.
BROADCASTER_ONLY = {
    ("DELETE", "/api/v1/channels/{login}"),
    ("POST", "/api/v1/channels/{login}/backfill"),
    ("DELETE", "/api/v1/channels/{login}/backfill/{job_id}"),
}
# Routes no moderator or broadcaster may call, whatever the channel.
ADMIN_ONLY = {
    ("POST", "/api/v1/channels"),
    ("GET", "/api/v1/variable-limits"),
    ("PATCH", "/api/v1/variable-limits/default"),
    ("PATCH", "/api/v1/variable-limits/{kind}/{user}"),
    ("GET", "/api/v1/http-hosts"),
    ("PUT", "/api/v1/http-hosts/{pattern}"),
    ("DELETE", "/api/v1/http-hosts/{pattern}"),
    ("PUT", "/api/v1/http-hosts/{pattern}/secret"),
    ("DELETE", "/api/v1/http-hosts/{pattern}/secret"),
    ("PATCH", "/api/v1/http-limits"),
}


def test_every_private_route_says_who_may_use_it() -> None:
    """A new route without an area would be open to every moderator in every channel. This fails first."""
    areas: dict[tuple[str, str], str] = {}
    ranks: dict[tuple[str, str], int | None] = {}
    public: set[tuple[str, str]] = set()
    for route in router.routes:
        assert isinstance(route, APIRoute)
        found = [d.call for d in route.dependant.dependencies if hasattr(d.call, "area")]
        for method in route.methods:
            key = (method, route.path)
            if key in PUBLIC:
                assert found == [], f"{key} is public but checks an area"
                continue
            assert len(found) == 1, f"{key} says nothing about who may use it"
            areas[key], ranks[key] = found[0].area, found[0].min_rank
            if getattr(found[0], "public", False):
                public.add(key)
    assert {k for k, area in areas.items() if area == "admin"} == ADMIN_ONLY
    assert {k for k, area in areas.items() if area == "personal"} == PERSONAL
    assert {k for k, rank in ranks.items() if rank == BROADCASTER_RANK} == BROADCASTER_ONLY
    assert {rank for k, rank in ranks.items() if areas[k] == "channel" and k not in public} == {
        MODERATOR_RANK,
        BROADCASTER_RANK,
    }
    assert public == PUBLIC_WHEN_OPEN


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
    assert session["channel_roles"] == {CHANNEL_LOGIN: "moderator"}
    assert session["channel_ranks"] == {CHANNEL_LOGIN: MODERATOR_RANK}
    assert session["own_channel"] == {"login": "mod", "joined": False, "status": None, "tier": None}
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


async def test_a_moderator_reaches_what_chat_lets_them_and_no_more(
    client: httpx.AsyncClient, mod: dict[str, str]
) -> None:
    own = f"/api/v1/channels/{CHANNEL_LOGIN}"
    # As `!logsearch`, `!cc disable` and the run log in chat.
    assert (await client.get(f"{own}/runs")).status_code == 200
    assert (await client.get(f"{own}/messages", params={"q": "hi"})).status_code == 200
    assert (await client.get(f"{own}/backfill")).status_code == 200
    assert (
        await client.patch(f"{own}/publications/x", json={"enabled": False}, headers=mod)
    ).status_code == 404  # allowed, but nothing is published under that name
    assert (await client.patch(own, json={"public_log": False}, headers=mod)).json()["public_log"] is False
    # The broadcaster's: leaving, backfill, logging.
    left = await client.delete(own, headers=mod)
    assert (
        left.status_code == 403
        and left.json()["detail"] == f"only the broadcaster can do this in {CHANNEL_LOGIN}"
    )
    assert (await client.post(f"{own}/backfill", json={"gaps": True}, headers=mod)).status_code == 403
    refused = await client.patch(own, json={"history_backfill": True}, headers=mod)
    assert (
        refused.status_code == 403
        and refused.json()["detail"] == "only the broadcaster can change history_backfill"
    )
    # An admin's.
    assert (await client.post("/api/v1/channels", json={"login": "friend"}, headers=mod)).status_code == 403
    assert (await client.get("/api/v1/keys")).status_code == 403
    assert (await client.post("/api/v1/keys", json={"name": "mine"}, headers=mod)).status_code == 403

    explain = {"text": "ping", "context": "body", "as_user": "friend"}
    assert (
        await client.post("/api/v1/explain", json={**explain, "channel": CHANNEL_LOGIN})
    ).status_code == 200
    assert (await client.post("/api/v1/explain", json={**explain, "channel": OTHER_LOGIN})).status_code == 403


async def test_a_broadcaster_manages_their_channel_as_in_chat(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService]
) -> None:
    app = app_and_keys[0]
    session = app.state.admin_auth.login(
        role="moderator", user_id=CHANNEL_ID, user_login=CHANNEL_LOGIN, channels=frozenset({CHANNEL_LOGIN})
    )
    client.cookies.set(SESSION_COOKIE, session.token)
    csrf = {"X-CSRF-Token": session.csrf}
    body = (await client.get("/api/v1/session")).json()
    assert body["channel_roles"] == {CHANNEL_LOGIN: "broadcaster"}
    assert body["channel_ranks"] == {CHANNEL_LOGIN: BROADCASTER_RANK}
    assert body["own_channel"] == {
        "login": CHANNEL_LOGIN,
        "joined": True,
        "status": "joined",
        "tier": "basic",
    }

    own = f"/api/v1/channels/{CHANNEL_LOGIN}"
    patched = await client.patch(
        own, json={"log_enabled": False, "publish_min_role": "everyone"}, headers=csrf
    )
    assert patched.status_code == 200 and patched.json()["log_enabled"] is False
    assert (await client.post("/api/v1/channels", json={"login": "friend"}, headers=csrf)).status_code == 403
    assert (await client.delete(own, headers=csrf)).json()["status"] == "parted"
    entries = (await client.get("/api/v1/audit", params={"channel": CHANNEL_LOGIN})).json()["entries"]
    assert (entries[0]["actor_user_id"], entries[0]["via"]) == (CHANNEL_ID, "web")


async def test_a_custom_role_raises_a_moderators_rank_as_in_chat(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], mod: dict[str, str]
) -> None:
    policy = app_and_keys[0].state.policy
    actor = Actor(CHANNEL_ID, "chat")

    async def co_owner(repo: Any) -> None:
        role_id = await repo.create_role(CHANNEL_ID, "co-owner", BROADCASTER_RANK, actor)
        await repo.add_member(role_id, CHANNEL_ID, "co-owner", MOD_ID, "mod", None, actor)

    own = f"/api/v1/channels/{CHANNEL_LOGIN}"
    assert (await client.patch(own, json={"log_enabled": False}, headers=mod)).status_code == 403
    await policy.mutate(co_owner)
    assert (await client.get("/api/v1/session")).json()["channel_ranks"] == {CHANNEL_LOGIN: BROADCASTER_RANK}
    assert (await client.patch(own, json={"log_enabled": False}, headers=mod)).status_code == 200


@pytest.fixture
async def user(client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService]) -> dict[str, str]:
    """Sign `client` in as friend, who manages no channel the bot is in."""
    session = app_and_keys[0].state.admin_auth.login(role="user", user_id="200", user_login="friend")
    client.cookies.set(SESSION_COOKIE, session.token)
    return {"X-CSRF-Token": session.csrf}


async def test_a_plain_user_adds_the_bot_to_their_own_channel(
    client: httpx.AsyncClient, user: dict[str, str]
) -> None:
    before = (await client.get("/api/v1/session")).json()
    assert (before["role"], before["channels"], before["channel_roles"]) == ("user", [], {})
    assert before["own_channel"] == {"login": "friend", "joined": False, "status": None, "tier": None}
    assert (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/runs")).status_code == 403
    assert (await client.get("/api/v1/audit")).json()["entries"] == []

    joined = await client.post("/api/v1/me/channel", headers=user)
    assert joined.status_code == 201 and joined.json()["login"] == "friend"
    after = (await client.get("/api/v1/session")).json()
    assert (after["role"], after["channels"], after["channel_roles"]) == (
        "moderator",
        ["friend"],
        {"friend": "broadcaster"},
    )
    assert after["own_channel"]["joined"] is True
    assert (await client.get("/api/v1/channels/friend")).status_code == 200
    assert (await client.post("/api/v1/me/channel")).status_code == 403  # a write needs the CSRF token


async def test_a_banned_channel_needs_an_admin_to_rejoin(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], user: dict[str, str]
) -> None:
    async def banned(repo: Any) -> None:
        await repo.ensure_channel("200", "friend", Actor(None, "test"))
        await repo.set_channel_field("200", "status", "banned", Actor(None, "test"))

    await app_and_keys[0].state.policy.mutate(banned)
    refused = await client.post("/api/v1/me/channel", headers=user)
    assert refused.status_code == 409 and "admin" in refused.json()["detail"]
    assert (await client.get("/api/v1/session")).json()["role"] == "user"


async def test_the_password_has_no_channel_of_its_own(client: httpx.AsyncClient) -> None:
    csrf = {
        "X-CSRF-Token": (await client.post("/api/v1/session", json={"password": PASSWORD})).json()["csrf"]
    }
    session = (await client.get("/api/v1/session")).json()
    assert (session["channel_roles"], session["channel_ranks"], session["own_channel"]) == (None, None, None)
    assert (await client.post("/api/v1/me/channel", headers=csrf)).status_code == 400


async def test_a_public_log_is_read_without_signing_in(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], mod: dict[str, str]
) -> None:
    app = app_and_keys[0]
    own = f"/api/v1/channels/{CHANNEL_LOGIN}"
    client.cookies.clear()
    assert (await client.get(f"{own}/messages", params={"q": "hi"})).status_code == 200
    assert (await client.get(f"{own}/log")).status_code == 200
    moderation = await client.get(f"{own}/log", params={"kind": "moderation"})
    assert moderation.status_code == 403
    assert (await client.get(f"{own}/runs")).status_code == 401  # only the log is public

    app.state.public_read_limiter = ReadLimiter(reads=1)
    assert (await client.get(f"{own}/log")).status_code == 200
    limited = await client.get(f"{own}/log")
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0

    await app.state.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "public_log", False, Actor(None, "test"))
    )
    app.state.public_read_limiter = ReadLimiter()
    assert (await client.get(f"{own}/messages", params={"q": "hi"})).status_code == 401
    assert (await client.get(f"{own}/log")).status_code == 401
    await app.state.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "public_log", True, Actor(None, "test"))
    )
    await app.state.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "log_enabled", False, Actor(None, "test"))
    )
    assert (await client.get(f"{own}/log")).status_code == 401  # nothing is public while logging is off


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


async def test_the_dev_api_signs_in_a_moderator_and_the_bot_has_no_such_route(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService]
) -> None:
    """`scripts/dev_api.py` signs in any made-up user without Twitch, on its own app only."""
    assert (await client.get("/dev/login-as", params={"user": "alice"})).status_code == 404
    with pytest.raises(SystemExit):
        parse_moderators(["alice:somewhere-else"])

    add_dev_login(app_and_keys[0], parse_moderators(["alice:doomtp"]))
    signed_in = await client.get("/dev/login-as", params={"user": "alice", "next": "//evil.example/"})
    assert signed_in.status_code == 302 and signed_in.headers["location"] == "/admin"
    session = (await client.get("/api/v1/session")).json()
    assert (session["role"], session["user"]["login"], session["channels"]) == (
        "moderator",
        "alice",
        ["doomtp"],
    )
    pest = await client.get("/dev/login-as", params={"user": "pest"})
    assert pest.text == "signed in as pest, managing no channel\n"
    assert (await client.get("/api/v1/session")).json()["role"] == "user"
    broadcaster = await client.get("/dev/login-as", params={"user": "vexoulz"})
    assert broadcaster.text == "signed in as vexoulz, managing vexoulz\n"
    assert (await client.get("/dev/login-as", params={"user": "nobody"})).status_code == 404
