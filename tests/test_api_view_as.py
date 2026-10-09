"""View as (ADR-0030): an admin session sends `X-View-As` and reads come back as that viewer gets them,
scoped on the server; every write is refused while the header is there."""
# ruff: noqa: F811  (the imported fixtures are parameters here, which ruff reads as redefinitions)

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from doomtp_bot.api.access import ViewAs, parse_view_as
from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.api.sessions import SESSION_COOKIE
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.roles import BOT_ADMIN_RANK, BROADCASTER_RANK, MODERATOR_RANK
from tests.test_api_data import (  # noqa: F401  (fixtures)
    CHANNEL_ID,
    CHANNEL_LOGIN,
    app_and_keys,
    auth,
    client,
    write_key,
)

ADMIN_ID, OTHER_ID, OTHER_LOGIN = "900", "700", "elsewhere"
OWN = f"/api/v1/channels/{CHANNEL_LOGIN}"


def as_(value: str) -> dict[str, str]:
    return {"X-View-As": value}


@pytest.fixture
async def admin(client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService]) -> dict[str, str]:
    """Sign `client` in as a bot admin through Twitch, with a second channel the previews must not see,
    and one change of the admin's own in that channel. Returns the CSRF header."""
    app = app_and_keys[0]
    await app.state.policy.mutate(lambda repo: repo.ensure_channel(OTHER_ID, OTHER_LOGIN, Actor(None, "test")))
    session = app.state.admin_auth.login(role="admin", user_id=ADMIN_ID, user_login="boss")
    client.cookies.set(SESSION_COOKIE, session.token)
    csrf = {"X-CSRF-Token": session.csrf}
    other = f"/api/v1/channels/{OTHER_LOGIN}"
    assert (await client.patch(other, json={"quiet_errors": True}, headers=csrf)).status_code == 200
    return csrf


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("signed-out", ViewAs("signed-out")),
        (" User ", ViewAs("user")),
        ("moderator@doomtp", ViewAs("moderator", "doomtp", MODERATOR_RANK)),
        ("broadcaster@#DoomTP", ViewAs("broadcaster", "doomtp", BROADCASTER_RANK)),
        ("45@doomtp", ViewAs("custom", "doomtp", 45)),
    ],
)
def test_the_header_names_a_viewer(value: str, expected: ViewAs) -> None:
    assert parse_view_as(value) == expected
    assert parse_view_as(expected.header) == expected


@pytest.mark.parametrize("value", ["", "admin", "moderator", "moderator@", "0@doomtp", "100@doomtp", "x@do/omtp"])
def test_anything_else_is_refused(value: str) -> None:
    with pytest.raises(HTTPException) as refused:
        parse_view_as(value)
    assert refused.value.status_code == 400


async def test_without_the_header_an_admin_sees_everything(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    channels = (await client.get("/api/v1/channels")).json()["channels"]
    assert {c["login"] for c in channels} == {CHANNEL_LOGIN, OTHER_LOGIN}
    assert (await client.get(f"{OWN}/roles")).json()["your_rank"] == BOT_ADMIN_RANK


async def test_as_a_moderator_reads_are_scoped_to_their_channel(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    view = as_(f"moderator@{CHANNEL_LOGIN}")
    session = (await client.get("/api/v1/session", headers=view)).json()
    assert session["view_as"] == f"moderator@{CHANNEL_LOGIN}"
    assert (session["authenticated"], session["role"], session["channels"]) == (True, "moderator", [CHANNEL_LOGIN])
    assert session["channel_roles"] == {CHANNEL_LOGIN: "moderator"}
    assert session["channel_ranks"] == {CHANNEL_LOGIN: MODERATOR_RANK}
    assert session["own_channel"] is None
    assert session["user"] == {"id": ADMIN_ID, "login": "boss"}

    channels = (await client.get("/api/v1/channels", headers=view)).json()["channels"]
    assert [c["login"] for c in channels] == [CHANNEL_LOGIN]
    assert (await client.get(f"/api/v1/channels/{OTHER_LOGIN}", headers=view)).status_code == 403
    assert (await client.get(f"{OWN}/runs", headers=view)).status_code == 200
    assert (await client.get(f"{OWN}/roles", headers=view)).json()["your_rank"] == MODERATOR_RANK
    assert (await client.get("/api/v1/admins", headers=view)).status_code == 403
    assert (await client.get("/api/v1/keys", headers=view)).status_code == 403
    # The admin's change in the other channel is not in this moderator's audit, nor reachable by channel.
    entries = (await client.get("/api/v1/audit", headers=view)).json()["entries"]
    assert all(e["channel_id"] == CHANNEL_ID for e in entries)
    assert (await client.get("/api/v1/audit", params={"channel": OTHER_LOGIN}, headers=view)).status_code == 403
    v2 = (await client.get("/api/v2/audit", headers=view)).json()["items"]
    assert all(row.get("scope_name") != OTHER_LOGIN for row in v2)


async def test_as_the_broadcaster_their_channel_is_their_own(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    view = as_(f"broadcaster@{CHANNEL_LOGIN}")
    session = (await client.get("/api/v1/session", headers=view)).json()
    assert session["channel_roles"] == {CHANNEL_LOGIN: "broadcaster"}
    assert session["channel_ranks"] == {CHANNEL_LOGIN: BROADCASTER_RANK}
    assert session["own_channel"] == {"login": CHANNEL_LOGIN, "joined": True, "status": "joined", "tier": "basic"}
    assert (await client.get(f"{OWN}/roles", headers=view)).json()["your_rank"] == BROADCASTER_RANK


async def test_as_a_custom_role_the_channel_is_not_theirs_to_manage(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    view = as_(f"45@{CHANNEL_LOGIN}")
    session = (await client.get("/api/v1/session", headers=view)).json()
    assert (session["role"], session["channels"], session["channel_ranks"]) == ("user", [], {CHANNEL_LOGIN: 45})
    assert (await client.get("/api/v1/channels", headers=view)).json()["channels"] == []
    assert (await client.get(f"{OWN}/runs", headers=view)).status_code == 403


async def test_as_a_plain_user_only_their_own_changes_show(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    view = as_("user")
    session = (await client.get("/api/v1/session", headers=view)).json()
    assert (session["role"], session["channels"], session["channel_roles"]) == ("user", [], {})
    assert (await client.get("/api/v1/channels", headers=view)).json()["channels"] == []
    entries = (await client.get("/api/v1/audit", headers=view)).json()["entries"]
    assert entries and all(e["actor_user_id"] == ADMIN_ID for e in entries)
    assert (await client.get("/api/v1/me/custom-commands", headers=view)).status_code == 200


async def test_as_someone_signed_out_private_reads_are_refused_and_marked(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    view = as_("signed-out")
    session = (await client.get("/api/v1/session", headers=view)).json()
    assert (session["authenticated"], session["csrf"], session["role"], session["user"]) == (False, None, None, None)
    assert session["view_as"] == "signed-out"
    refused = await client.get("/api/v1/channels", headers=view)
    assert refused.status_code == 401 and refused.headers["X-View-As"] == "signed-out"
    # Public pages answer as for anyone.
    assert (await client.get("/api/v1/custom-commands", headers=view)).status_code == 200
    # The session itself is untouched.
    assert (await client.get("/api/v1/session")).json()["authenticated"] is True


@pytest.mark.parametrize("value", ["signed-out", "user", f"moderator@{CHANNEL_LOGIN}", f"broadcaster@{CHANNEL_LOGIN}"])
async def test_every_write_is_refused_while_previewing(
    client: httpx.AsyncClient, admin: dict[str, str], value: str
) -> None:
    headers = {**admin, **as_(value)}
    for refused in (
        await client.patch(OWN, json={"quiet_errors": False}, headers=headers),
        await client.put(f"{OWN}/modules/basic", json={"enabled": False}, headers=headers),
        await client.post("/api/v1/keys", json={"name": "x"}, headers=headers),
        await client.post("/api/v1/me/channel", headers=headers),
    ):
        assert refused.status_code == 403, refused.json()
        assert "read-only while viewing as" in refused.json()["detail"]
    assert (await client.get(OWN)).json()["quiet_errors"] is False  # nothing applied


async def test_only_an_admin_session_may_send_the_header(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    keyed = await client.get("/api/v1/channels", headers={**auth(write_key), **as_("user")})
    assert keyed.status_code == 400
    session = app_and_keys[0].state.admin_auth.login(
        role="moderator", user_id="300", user_login="mod", channels=frozenset({CHANNEL_LOGIN})
    )
    client.cookies.set(SESSION_COOKIE, session.token)
    assert (await client.get("/api/v1/channels", headers=as_("user"))).status_code == 403
    assert (await client.get("/api/v1/session", headers=as_("user"))).status_code == 403


async def test_an_unknown_channel_or_value_is_a_bad_request(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    assert (await client.get("/api/v1/channels", headers=as_("moderator@nobody"))).status_code == 400
    assert (await client.get("/api/v1/session", headers=as_("owner"))).status_code == 400
