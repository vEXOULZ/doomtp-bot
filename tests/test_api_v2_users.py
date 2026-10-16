"""/api/v2/twitch/users: a Twitch user's public profile for the chat user cards (ADR-0035)."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport
from vex_platform.api import install_error_handlers

from doomtp_bot.api import v2_users

URL = "/api/v2/twitch/users"
VODS = "https://vods.example"

ALICE = {
    "id": "111",
    "login": "alice",
    "display_name": "Alice",
    "type": None,
    "broadcaster_type": "affiliate",
    "description": "hi",
    "profile_image_url": "https://static-cdn.example/alice.png",
    "created_at": dt.datetime(2015, 3, 1, 12, 0, tzinfo=dt.UTC),
}


class ProfileTwitch:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.fail = False

    async def fetch_profile(self, **by: str) -> dict[str, Any] | None:
        self.calls.append(by)
        if self.fail:
            raise RuntimeError("helix is down")
        if by.get("user_id") == "111" or by.get("login") == "alice":
            return dict(ALICE)
        return None


@pytest.fixture
def twitch() -> ProfileTwitch:
    return ProfileTwitch()


@pytest.fixture
async def client(twitch: ProfileTwitch) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    install_error_handlers(app, "/api/v2")
    v2_users.install(app, v2_users.parse_origins(f"{VODS}/, https://other.example"))
    v2 = APIRouter(prefix="/api/v2")
    v2.include_router(v2_users.users_router())
    app.include_router(v2)
    app.state.twitch = twitch
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def test_a_profile_by_id_or_login_with_an_iso_time(client: httpx.AsyncClient, twitch: ProfileTwitch) -> None:
    by_id = await client.get(URL, params={"id": "111", "login": "ignored"})
    assert by_id.status_code == 200, by_id.text
    body = by_id.json()
    assert (body["id"], body["login"], body["broadcaster_type"], body["type"]) == ("111", "alice", "affiliate", None)
    assert body["created_at"].startswith("2015-03-01T12:00:00") and body["created_at"].endswith("Z")
    assert "email" not in body
    # The id was preferred, and the answer serves the same user asked by login too.
    assert (await client.get(URL, params={"login": "@Alice"})).json() == body
    assert twitch.calls == [{"user_id": "111"}]


async def test_no_such_user_is_a_cached_404(client: httpx.AsyncClient, twitch: ProfileTwitch) -> None:
    for _ in range(2):
        missing = await client.get(URL, params={"login": "nobody"})
        assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
    assert twitch.calls == [{"login": "nobody"}]


async def test_bad_or_missing_keys_are_refused(client: httpx.AsyncClient) -> None:
    assert (await client.get(URL)).status_code == 422
    assert (await client.get(URL, params={"login": "not a login"})).status_code == 422
    assert (await client.get(URL, params={"id": "12ab"})).status_code == 422


async def test_twitch_failing_is_unavailable_and_not_remembered(
    client: httpx.AsyncClient, twitch: ProfileTwitch
) -> None:
    twitch.fail = True
    refused = await client.get(URL, params={"id": "111"})
    assert (refused.status_code, refused.json()["code"]) == (503, "unavailable")
    twitch.fail = False
    assert (await client.get(URL, params={"id": "111"})).status_code == 200


async def test_listed_origins_may_read_it_errors_included(client: httpx.AsyncClient) -> None:
    found = await client.get(URL, params={"id": "111"}, headers={"Origin": VODS})
    assert found.headers["access-control-allow-origin"] == VODS
    missing = await client.get(URL, params={"login": "nobody"}, headers={"Origin": VODS})
    assert missing.status_code == 404 and missing.headers["access-control-allow-origin"] == VODS
    stranger = await client.get(URL, params={"id": "111"}, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in stranger.headers
    assert "access-control-allow-credentials" not in found.headers


async def test_lookups_that_reach_twitch_are_rate_limited(client: httpx.AsyncClient) -> None:
    for i in range(v2_users.LOOKUPS_PER_MINUTE):
        assert (await client.get(URL, params={"id": str(1000 + i)})).status_code == 404
    limited = await client.get(URL, params={"id": "999999"})
    assert (limited.status_code, limited.json()["code"]) == (429, "rate_limited")
    assert int(limited.headers["retry-after"]) >= 1
    assert (await client.get(URL, params={"id": "1000"})).status_code == 404  # cached: not counted
