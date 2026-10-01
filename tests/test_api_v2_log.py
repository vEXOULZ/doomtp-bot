"""/api/v2/channels/{login}/log and /log/coverage: v1's timeline in v2's shape (ADR-0025, ADR-0027)."""
# ruff: noqa: F811  (the imported fixtures are parameters here, which ruff reads as redefinitions)

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.api.routes.data import _log_reader
from doomtp_bot.api.v2_log import log_router
from doomtp_bot.policy.repository import Actor
from doomtp_bot.storage.db import Connection
from tests.test_api_data import (  # noqa: F401  (fixtures)
    CHANNEL_ID,
    CHANNEL_LOGIN,
    app_and_keys,
    auth,
    client,
    write_key,
)
from tests.test_chatlog_timeline import T, _session, chatlog  # noqa: F401  (fixtures)

LOG = f"/api/v2/channels/{CHANNEL_LOGIN}/log"


def iso(ms: int) -> str:
    return (
        dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def same_time(value: str, ms: int) -> bool:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) == dt.datetime.fromtimestamp(
        ms / 1000, dt.UTC
    )


def _ids(entries: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return [(e["kind"], e["id"]) for e in entries]


def test_the_log_routes_are_v1s_log_reader() -> None:
    """Moderators of the channel, or anyone while its log is public, as v1's (ADR-0026)."""
    for route in log_router().routes:
        calls = [d.call for d in route.dependant.dependencies]  # type: ignore[attr-defined]
        assert calls == [_log_reader], route.path  # type: ignore[attr-defined]


async def test_the_log_is_one_timeline_with_iso_times(
    client: httpx.AsyncClient, chatlog: Connection, write_key: str
) -> None:
    response = await client.get(LOG, headers=auth(write_key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "next_cursor"} and body["next_cursor"] is None
    assert [kind for kind, _ in _ids(body["items"])] == [
        "message", "moderation", "message", "notification", "message", "message",
    ]  # fmt: skip
    newest, timeout, deleted, sub = body["items"][:4]
    assert newest["at"].endswith("Z") and same_time(newest["at"], T + 30)
    assert same_time(deleted["deleted_at"], T + 20) and deleted["cleared_at"] is None
    assert same_time(deleted["received_at"], T + 15)
    assert (timeout["type"], timeout["duration_s"], timeout["target"]["login"]) == ("timeout", 60, "alice")
    assert sub["payload"] == {"tier": "1000"} and sub["user"]["display_name"] == "Alice"


async def test_pages_follow_on_and_times_bound_the_window(
    client: httpx.AsyncClient, chatlog: Connection, write_key: str
) -> None:
    whole = (await client.get(LOG, params={"order": "asc"}, headers=auth(write_key))).json()["items"]
    found: list[dict[str, Any]] = []
    cursor = None
    while True:
        params = {"order": "asc", "limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get(LOG, params=params, headers=auth(write_key))).json()
        found += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert _ids(found) == _ids(whole)

    window = {"since": iso(T + 10), "until": iso(T + 30), "order": "asc"}
    inside = (await client.get(LOG, params=window, headers=auth(write_key))).json()["items"]
    assert [e["kind"] for e in inside] == ["message", "moderation"]  # m-c and the timeout, not m-d at T + 30

    bad = await client.get(LOG, params={"cursor": "nonsense"}, headers=auth(write_key))
    assert bad.status_code == 400 and bad.headers["content-type"] == "application/problem+json"
    assert bad.json()["code"] == "bad_cursor"
    missing = await client.get("/api/v2/channels/nobody/log", headers=auth(write_key))
    assert (missing.status_code, missing.json()["code"]) == (404, "unknown_channel")


async def test_a_public_log_shows_what_chat_saw_and_can_be_closed(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], chatlog: Connection
) -> None:
    body = (await client.get(LOG)).json()
    assert [e["kind"] for e in body["items"]] == ["message", "notification", "message", "message"]
    refused = await client.get(LOG, params={"kind": "moderation"})
    assert (refused.status_code, refused.json()["code"]) == (403, "forbidden")

    await app_and_keys[0].state.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "public_log", False, Actor(None, "test"))
    )
    closed = await client.get(LOG)
    assert closed.status_code == 401 and closed.headers["content-type"] == "application/problem+json"
    assert (await client.get(f"{LOG}/coverage", params={"since": iso(T)})).status_code == 401


async def test_coverage_names_its_gaps_in_iso_times(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    conn: Connection = app_and_keys[0].state.chatlog
    await _session(conn, T, T + 60_000, "update")
    await _session(conn, T + 90_000, None)

    params = {"since": iso(T - 5_000), "until": iso(T + 120_000)}
    body = (await client.get(f"{LOG}/coverage", params=params, headers=auth(write_key))).json()
    assert same_time(body["since"], T - 5_000) and same_time(body["until"], T + 120_000)
    assert [(g["reason"], g["backfill"]) for g in body["gaps"]] == [
        ("before_log", None),
        ("between_sessions", None),
    ]
    between = body["gaps"][1]
    assert same_time(between["start"], T + 60_000) and same_time(between["end"], T + 90_000)
    assert body["complete"] is False
    assert [s["end_reason"] for s in body["sessions"]] == ["update", None]
    assert body["sessions"][1]["ended_at"] is None

    await conn.execute(
        "INSERT INTO backfill_runs (channel_id, gap_from, gap_to, inserted, complete, provider, job_id, at)"
        " VALUES (%s, %s, %s, 3, TRUE, 'ivr', 7, %s)",
        (CHANNEL_ID, T + 60_000, T + 90_000, T + 100_000),
    )
    body = (await client.get(f"{LOG}/coverage", params=params, headers=auth(write_key))).json()
    assert body["gaps"][1]["backfill"]["job_id"] == 7

    backwards = {"since": iso(T), "until": iso(T)}
    refused = await client.get(f"{LOG}/coverage", params=backwards, headers=auth(write_key))
    assert (refused.status_code, refused.json()["code"]) == (422, "invalid")


class BadgesTwitch:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    async def fetch_badges(self, channel_id: str) -> dict[str, list[dict[str, Any]]]:
        self.calls.append(channel_id)
        if self.fail:
            raise RuntimeError("helix is down")
        version = {"id": "1", "image_url_1x": "a", "image_url_2x": "b", "image_url_4x": "c", "title": "Sub"}
        return {"channel": [{"set_id": "subscriber", "versions": [version]}], "global": []}


async def test_badges_are_twitchs_cached_for_whoever_reads_the_log(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    app = app_and_keys[0]
    twitch = app.state.twitch = BadgesTwitch()
    url = f"/api/v2/channels/{CHANNEL_LOGIN}/badges"
    first = await client.get(url)  # the fixture's log is public
    assert first.status_code == 200
    assert first.json()["channel"][0]["set_id"] == "subscriber" and first.json()["global"] == []
    twitch.fail = True
    assert (await client.get(url, headers=auth(write_key))).json() == first.json()
    assert twitch.calls == [CHANNEL_ID]  # the second came from the cache

    await app.state.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "public_log", False, Actor(None, "test"))
    )
    assert (await client.get(url)).status_code == 401


async def test_badges_are_unavailable_when_twitch_fails_with_nothing_cached(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    app_and_keys[0].state.twitch = twitch = BadgesTwitch()
    twitch.fail = True
    refused = await client.get(f"/api/v2/channels/{CHANNEL_LOGIN}/badges", headers=auth(write_key))
    assert (refused.status_code, refused.json()["code"]) == (503, "unavailable")
