"""Twitch integration without Twitch: payload mapping, dedupe, OAuth flow and routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from doomtp_bot.api.app import create_app
from doomtp_bot.core.events import ChatMessage, Event
from doomtp_bot.core.health import HealthRegistry
from doomtp_bot.storage.db import Databases
from doomtp_bot.twitch import mapping
from doomtp_bot.twitch.auth import BOT_SCOPES, AuthorizedAccount, OAuthError, TwitchAuth
from doomtp_bot.twitch.client import TwitchService
from doomtp_bot.twitch.tokens import TokenStore

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

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        self.codes.append(code)
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 14000, "scope": self.scopes}

    async def validate(self, access_token: str) -> dict[str, Any]:
        return {"user_id": "999", "login": "doomtp_bot", "scopes": self.scopes}


@pytest.fixture
async def dbs(tmp_path: Path) -> AsyncIterator[Databases]:
    databases = await Databases.open(tmp_path / "bot.db", tmp_path / "chatlog.db")
    yield databases
    await databases.close()


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
