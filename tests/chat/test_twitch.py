"""Twitch integration without Twitch: payload mapping, dedupe, OAuth flow and routes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from doomtp_bot.api.app import create_app
from doomtp_bot.core.capabilities import granted_by
from doomtp_bot.core.events import ChatMessage, Event
from doomtp_bot.core.health import HealthRegistry
from doomtp_bot.storage.db import Databases
from doomtp_bot.twitch import mapping
from doomtp_bot.twitch.auth import (
    BOT_SCOPES,
    BROADCASTER_SCOPES,
    AuthorizedAccount,
    OAuthError,
    TwitchAuth,
)
from doomtp_bot.twitch.client import BroadcasterEvents, TwitchService
from doomtp_bot.twitch.tokens import TokenStore, broadcaster_identity

WHEN = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def user(uid: str, login: str) -> NS:
    return NS(id=uid, name=login, display_name=login.title())


def fake_chat_message(**overrides: Any) -> NS:
    base = dict(
        id="m1",
        broadcaster=user("100", "doomtp"),
        chatter=user("400", "alice"),
        text="@bob !roll 20",
        timestamp=WHEN,
        badges=[NS(set_id="moderator", id="1", info=None)],
        fragments=[NS(type="mention", text="@bob", mention=user("401", "bob"), emote=None, cheermote=None),
                   NS(type="text", text=" !roll 20", mention=None, emote=None, cheermote=None)],
        type="text",
        cheer=None,
        reply=NS(parent_message_id="p1", parent_user=user("401", "bob")),
        channel_points_id=None,
        source_broadcaster=None,
    )  # fmt: skip
    base.update(overrides)
    return NS(**base)


def test_map_chat_message() -> None:
    event = mapping.chat_message(fake_chat_message(), bot_id="999")
    assert isinstance(event, ChatMessage)
    assert (event.message_id, event.channel_id, event.user_id, event.user_login) == (
        "m1",
        "100",
        "400",
        "alice",
    )
    assert event.sent_at == int(WHEN.timestamp() * 1000)
    assert event.reply_parent_login == "bob" and event.reply_parent_id == "p1"
    assert event.badges[0].set_id == "moderator" and event.fragments[0]["mention"] == {
        "id": "401",
        "login": "bob",
    }
    assert not event.is_self
    assert mapping.chat_message(fake_chat_message(chatter=user("999", "doomtp_bot")), bot_id="999").is_self


def test_map_moderation_and_notification() -> None:
    deleted = mapping.message_deleted(
        NS(broadcaster=user("100", "c"), user=user("400", "a"), message_id="m1", timestamp=WHEN)
    )
    assert (deleted.message_id, deleted.target_user_id) == ("m1", "400")
    cleared = mapping.user_messages_cleared(
        NS(broadcaster=user("100", "c"), user=user("400", "a"), timestamp=None)
    )
    assert cleared.target_user_id == "400" and cleared.at > 0
    raid = NS(user=user("500", "raider"), viewer_count=42, profile_image=None)
    note = mapping.chat_notification(
        NS(id="n1", broadcaster=user("100", "c"), chatter=user("500", "raider"), anonymous=False, notice_type="raid",
           raid=raid, system_message="raider is raiding with 42 viewers", text="", timestamp=WHEN)
    )  # fmt: skip
    assert note.type == "raid" and note.user_id == "500"
    assert note.payload["detail"]["user"] == {"id": "500", "login": "raider", "display": "Raider"}


async def test_eventsub_redeliveries_are_dropped(tmp_path: Path) -> None:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    service = TwitchService(client_id="x", client_secret="y", tokens=None, sink=sink)  # type: ignore[arg-type]
    event = mapping.chat_message(fake_chat_message(), bot_id=None)
    await service.emit("m1", event)
    await service.emit("m1", event)
    await service.emit(None, event)
    assert len(received) == 2
    assert (await service.health()).status.value == "degraded"
    assert await service.send_chat("100", "hi", None) == type(await service.send_chat("100", "hi", None))(
        None, "not_connected"
    )


# ── OAuth ──────────────────────────────────────────────────────────────────
class FakeOAuthHttp:
    def __init__(self, scopes: list[str] | None = None) -> None:
        self.scopes = scopes if scopes is not None else list(BOT_SCOPES)
        self.codes: list[str] = []
        self.user = {"user_id": "999", "login": "doomtp_bot"}

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        self.codes.append(code)
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 14000, "scope": self.scopes}

    async def validate(self, access_token: str) -> dict[str, Any]:
        return {**self.user, "scopes": self.scopes}


async def test_oauth_flow_stores_token_and_notifies(dbs: Databases) -> None:
    authorized: list[AuthorizedAccount] = []

    async def on_authorized(account: AuthorizedAccount) -> None:
        authorized.append(account)

    tokens = TokenStore(dbs.bot)
    auth = TwitchAuth(client_id="cid", redirect_uri="http://localhost:8080/auth/callback", tokens=tokens,
                      http=FakeOAuthHttp(), on_bot_authorized=on_authorized)  # fmt: skip
    url = urlsplit(auth.login_url())
    query = parse_qs(url.query)
    assert url.netloc == "id.twitch.tv" and query["client_id"] == ["cid"]
    assert "user:read:chat" in query["scope"][0].split(" ")

    with pytest.raises(OAuthError, match="invalid or expired"):
        await auth.complete("code", "forged-state")

    account = await auth.complete("code", query["state"][0])
    assert account.login == "doomtp_bot" and authorized == [account]
    stored = await tokens.get()
    assert stored is not None and (stored.user_id, stored.refresh_token) == ("999", "rt")

    with pytest.raises(OAuthError):  # states are single-use
        await auth.complete("code", query["state"][0])


async def test_oauth_rejects_missing_chat_scopes(dbs: Databases) -> None:
    auth = TwitchAuth(
        client_id="cid", redirect_uri="r", tokens=TokenStore(dbs.bot), http=FakeOAuthHttp(["user:bot"])
    )
    state = parse_qs(urlsplit(auth.login_url()).query)["state"][0]
    with pytest.raises(OAuthError, match="user:read:chat"):
        await auth.complete("code", state)


async def test_oauth_rejects_wrong_account(dbs: Databases) -> None:
    tokens = TokenStore(dbs.bot)
    auth = TwitchAuth(
        client_id="cid", redirect_uri="r", tokens=tokens, http=FakeOAuthHttp(), expected_bot_id="123"
    )
    state = parse_qs(urlsplit(auth.login_url()).query)["state"][0]
    with pytest.raises(OAuthError, match="sign in as the bot account"):
        await auth.complete("code", state)
    assert await tokens.get() is None  # nothing stored for the wrong account


async def test_auth_routes(dbs: Databases) -> None:
    auth = TwitchAuth(client_id="cid", redirect_uri="r", tokens=TokenStore(dbs.bot), http=FakeOAuthHttp())
    app = create_app(HealthRegistry(), auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.get("/auth/login")
        assert login.status_code == 302 and login.headers["location"].startswith("https://id.twitch.tv/")
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        bad = await client.get("/auth/callback", params={"code": "c", "state": "nope"})
        assert bad.status_code == 400 and "try again" in bad.text
        ok = await client.get("/auth/callback", params={"code": "c", "state": state})
        assert ok.status_code == 200 and "doomtp_bot" in ok.text

    unconfigured = create_app(HealthRegistry())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unconfigured), base_url="http://test"
    ) as client:
        assert (await client.get("/auth/login")).status_code == 503


# ── the broadcaster connect flow (ADR-0007 item 5) ─────────────────────────
def test_map_redemption_and_cheer() -> None:
    redeemed = mapping.redemption(
        NS(id="r1", broadcaster=user("100", "doomtp"), user=user("400", "alice"), redeemed_at=WHEN,
           user_input="a song please", status="unfulfilled",
           reward=NS(id="rw1", title="Song request", cost=500))
    )  # fmt: skip
    assert (redeemed.type, redeemed.user_id) == ("redemption", "400")
    assert redeemed.payload["reward"] == {"id": "rw1", "title": "Song request", "cost": 500}
    assert redeemed.payload["input"] == "a song please"

    cheered = mapping.cheer(
        NS(broadcaster=user("100", "doomtp"), user=user("400", "alice"), anonymous=False, bits=300,
           message="cheer300 nice", timestamp=WHEN)
    )  # fmt: skip
    assert (cheered.type, cheered.payload["bits"]) == ("cheer", 300)
    assert cheered.payload["user"]["name"] == "alice"

    anonymous = mapping.cheer(
        NS(broadcaster=user("100", "doomtp"), user=None, anonymous=True, bits=100, message="", timestamp=WHEN)
    )
    assert anonymous.user_id is None and anonymous.payload["user"] is None
    assert "someone cheered 100 bits" in anonymous.payload["system_message"]


async def test_a_broadcaster_connects_their_own_channel(dbs: Databases) -> None:
    connected: list[AuthorizedAccount] = []

    async def on_connected(account: AuthorizedAccount) -> None:
        connected.append(account)

    tokens = TokenStore(dbs.bot)
    http = FakeOAuthHttp(list(BROADCASTER_SCOPES))
    http.user = {"user_id": "100", "login": "doomtp"}
    auth = TwitchAuth(client_id="cid", redirect_uri="r", tokens=tokens, http=http,
                      on_broadcaster_authorized=on_connected, expected_bot_id="999")  # fmt: skip

    query = parse_qs(urlsplit(auth.connect_url()).query)
    assert set(query["scope"][0].split(" ")) == set(BROADCASTER_SCOPES)

    account = await auth.complete("code", query["state"][0])
    assert (account.flow, account.login) == ("broadcaster", "doomtp")
    assert connected == [account]
    stored = await tokens.get(broadcaster_identity("100"))
    assert stored is not None and stored.access_token == "at"
    assert await tokens.get() is None  # the bot's own token is untouched
    assert [t.login for t in await tokens.broadcasters()] == ["doomtp"]
    assert await tokens.forget(broadcaster_identity("100")) is True


async def test_a_broadcaster_who_grants_nothing_changes_nothing(dbs: Databases) -> None:
    tokens = TokenStore(dbs.bot)
    http = FakeOAuthHttp(["user:read:email"])
    http.user = {"user_id": "100", "login": "doomtp"}
    auth = TwitchAuth(client_id="cid", redirect_uri="r", tokens=tokens, http=http)
    state = parse_qs(urlsplit(auth.connect_url()).query)["state"][0]
    with pytest.raises(OAuthError, match="nothing was granted"):
        await auth.complete("code", state)
    assert await tokens.broadcasters() == []


def test_scopes_become_capabilities() -> None:
    assert granted_by(BROADCASTER_SCOPES) == frozenset({"redemptions", "subs", "bits"})
    assert granted_by(["bits:read"]) == frozenset({"bits"})
    assert granted_by(["channel:manage:redemptions"]) == frozenset({"redemptions"})
    assert granted_by(["channel:bot"]) == frozenset()  # the badge buys no events by itself


async def test_the_connect_route_redirects_and_reports_what_was_granted(dbs: Databases) -> None:
    http = FakeOAuthHttp(list(BROADCASTER_SCOPES))
    http.user = {"user_id": "100", "login": "doomtp"}
    auth = TwitchAuth(client_id="cid", redirect_uri="r", tokens=TokenStore(dbs.bot), http=http)
    app = create_app(HealthRegistry(), auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        connect = await client.get("/auth/connect")
        assert connect.status_code == 302
        state = parse_qs(urlsplit(connect.headers["location"]).query)["state"][0]
        done = await client.get("/auth/callback", params={"code": "c", "state": state})
        assert done.status_code == 200 and "Channel connected" in done.text
        assert "channel:read:redemptions" in done.text


class FakeSubscriptions:
    """Stands in for the TwitchIO client: it records subscriptions, or refuses them the same way."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.made: list[tuple[str, str]] = []

    async def subscribe_websocket(self, subscription: Any, token_for: str) -> None:
        if self.error is not None:
            raise self.error
        self.made.append((subscription.type, token_for))


async def test_what_comes_of_subscribing_with_a_broadcaster_token() -> None:
    """Only Twitch refusing the token means the grant is gone; a blip is just a blip (ADR-0007 item 5)."""

    async def sink(event: Event) -> None: ...

    service = TwitchService(client_id="x", client_secret="y", tokens=None, sink=sink)  # type: ignore[arg-type]

    happy = FakeSubscriptions()
    service.client = happy  # type: ignore[assignment]
    assert await service.subscribe_broadcaster("100", {"chat", "redemptions", "bits"}) == (
        BroadcasterEvents()
    )
    assert happy.made == [
        ("channel.channel_points_custom_reward_redemption.add", "100"),
        ("channel.cheer", "100"),
    ]

    service.client = FakeSubscriptions(RuntimeError("HTTPException: 401 Unauthorized"))  # type: ignore[assignment]
    refused = await service.subscribe_broadcaster("100", {"redemptions"})
    assert refused == BroadcasterEvents(
        ("channel.channel_points_custom_reward_redemption.add",), unauthorized=True
    )

    service.client = FakeSubscriptions(RuntimeError("409 Conflict: subscription already exists"))  # type: ignore[assignment]
    assert await service.subscribe_broadcaster("100", {"bits"}) == BroadcasterEvents()

    service.client = FakeSubscriptions(TimeoutError("the request timed out"))  # type: ignore[assignment]
    flaky = await service.subscribe_broadcaster("100", {"bits"})
    assert flaky.failed == ("channel.cheer",) and not flaky.unauthorized

    service.client = None
    assert await service.subscribe_broadcaster("100", {"bits"}) == BroadcasterEvents(("channel.cheer",))


class DeadClient:
    """A client that ends the way TwitchIO's does when EventSub gives up: by raising, or by returning."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def start(self, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error


async def test_a_client_that_stops_on_its_own_asks_to_be_started_again() -> None:
    """ADR-0001: nothing else notices the bot has gone deaf, so the client says so itself."""
    restarts: list[str] = []

    async def on_stopped() -> None:
        restarts.append("go")

    async def sink(event: Event) -> None: ...

    service = TwitchService(client_id="x", client_secret="y", tokens=None, sink=sink, on_stopped=on_stopped)  # type: ignore[arg-type]

    for client in (DeadClient(RuntimeError("websocket closed")), DeadClient()):
        service.client = client  # type: ignore[assignment]
        await service._run(client)  # type: ignore[arg-type]
    await asyncio.sleep(0)  # the restart runs in a task of its own, since it cancels this one
    assert restarts == ["go", "go"]
    assert service.last_error is not None

    stale = DeadClient()  # a task left over from an earlier client must not restart the current one
    await service._run(stale)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert restarts == ["go", "go"]
