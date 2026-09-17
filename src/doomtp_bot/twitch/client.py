"""TwitchService: owns the TwitchIO client — tokens from bot.db, EventSub subscriptions, sending, user lookups.

This is the only module that imports twitchio (ADR-0002).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
import twitchio
from twitchio import eventsub

from doomtp_bot.core.events import Event
from doomtp_bot.core.health import ComponentHealth, Status
from doomtp_bot.core.outbox import SendResult
from doomtp_bot.twitch import mapping
from doomtp_bot.twitch.tokens import BOT_IDENTITY, TokenStore

log = structlog.get_logger(__name__)

EventSink = Callable[[Event], Awaitable[None]]
DEDUPE_SIZE = 2000
USER_CACHE_SIZE = 5000

CHAT_SUBSCRIPTIONS: tuple[type[Any], ...] = (
    eventsub.ChatMessageSubscription,
    eventsub.ChatNotificationSubscription,
    eventsub.ChatMessageDeleteSubscription,
    eventsub.ChatClearSubscription,
    eventsub.ChatClearUserMessagesSubscription,
)


class _BotClient(twitchio.Client):
    """TwitchIO client wired to our token store and event sink."""

    def __init__(self, service: TwitchService, *, client_id: str, client_secret: str, bot_id: str) -> None:
        super().__init__(
            client_id=client_id, client_secret=client_secret, bot_id=bot_id, fetch_client_user=False
        )
        self.service = service

    async def load_tokens(self, path: str | None = None, /) -> None:
        stored = await self.service.tokens.get(BOT_IDENTITY)
        if stored is not None and stored.refresh_token:
            await self.add_token(stored.access_token, stored.refresh_token)

    async def save_tokens(self, path: str | None = None, /) -> None:
        return None  # tokens are persisted when added/refreshed, never to .tio.tokens.json

    async def event_token_refreshed(self, payload: Any) -> None:
        await self.service.tokens.update_refreshed(
            payload.user_id, payload.token, payload.refresh_token, payload.expires_in
        )
        log.info("twitch.token_refreshed", user_id=payload.user_id)

    async def event_message(self, payload: Any) -> None:
        await self.service.emit(payload.id, mapping.chat_message(payload, self.bot_id))

    async def event_chat_notification(self, payload: Any) -> None:
        await self.service.emit(payload.id, mapping.chat_notification(payload))

    async def event_message_delete(self, payload: Any) -> None:
        await self.service.emit(None, mapping.message_deleted(payload))

    async def event_chat_clear(self, payload: Any) -> None:
        await self.service.emit(None, mapping.chat_cleared(payload))

    async def event_chat_clear_user(self, payload: Any) -> None:
        await self.service.emit(None, mapping.user_messages_cleared(payload))


class TwitchService:
    def __init__(self, *, client_id: str, client_secret: str, tokens: TokenStore, sink: EventSink) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens = tokens
        self.sink = sink
        self.client: _BotClient | None = None
        self.bot_id: str | None = None
        self.bot_login: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._subscribed: set[str] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._users_by_login: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._logins_by_id: dict[str, str] = {}
        self.last_error: str | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> bool:
        """Start the client if a bot token exists. Returns False if the bot hasn't been authorized yet."""
        stored = await self.tokens.get(BOT_IDENTITY)
        if stored is None or not stored.refresh_token:
            log.warning("twitch.not_authorized", hint="open /auth/login to authorize the bot account")
            return False
        await self.stop()
        self.bot_id, self.bot_login = stored.user_id, stored.login
        self.client = _BotClient(
            self, client_id=self.client_id, client_secret=self.client_secret, bot_id=stored.user_id
        )
        self._subscribed.clear()
        await self.client.login()
        self._task = asyncio.create_task(self._run(self.client), name="twitch-client")
        log.info("twitch.started", bot=stored.login)
        return True

    async def _run(self, client: _BotClient) -> None:
        try:
            await client.start(with_adapter=False, load_tokens=False, save_tokens=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = repr(exc)
            log.exception("twitch.client_stopped")

    async def stop(self) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.close(save_tokens=False)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.client, self._task = None, None

    async def health(self) -> ComponentHealth:
        if self.client is None:
            return ComponentHealth(Status.DEGRADED, {"reason": "bot not authorized; open /auth/login"})
        if self._task is not None and self._task.done():
            return ComponentHealth(Status.UNHEALTHY, {"reason": "client stopped", "error": self.last_error})
        return ComponentHealth(Status.OK, {"bot": self.bot_login, "channels": len(self._subscribed)})

    # ── events ──────────────────────────────────────────────────────────────
    async def emit(self, dedupe_id: str | None, event: Event) -> None:
        """EventSub is at-least-once: drop repeats by message id (ADR-0001)."""
        if dedupe_id is not None:
            if dedupe_id in self._seen:
                return
            self._seen[dedupe_id] = None
            if len(self._seen) > DEDUPE_SIZE:
                self._seen.popitem(last=False)
        try:
            await self.sink(event)
        except Exception:
            log.exception("twitch.sink_failed", event=type(event).__name__)

    async def subscribe_channel(self, channel_id: str) -> list[str]:
        """Subscribe to basic-tier chat events for a channel. Returns the subscription types that failed."""
        if self.client is None or self.bot_id is None:
            return [s.type for s in CHAT_SUBSCRIPTIONS]
        failed: list[str] = []
        for subscription in CHAT_SUBSCRIPTIONS:
            try:
                await self.client.subscribe_websocket(
                    subscription(broadcaster_user_id=channel_id, user_id=self.bot_id), token_for=self.bot_id
                )
            except Exception as exc:
                failed.append(subscription.type)
                log.warning(
                    "twitch.subscribe_failed", channel=channel_id, type=subscription.type, error=repr(exc)
                )
        if not failed:
            self._subscribed.add(channel_id)
        return failed

    def is_subscribed(self, channel_id: str) -> bool:
        return channel_id in self._subscribed

    # ── Helix ───────────────────────────────────────────────────────────────
    async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
        if self.client is None or self.bot_id is None:
            return SendResult(None, "not_connected")
        channel = self.client.create_partialuser(channel_id)
        sent = await channel.send_message(
            text, self.bot_id, token_for=self.bot_id, reply_to_message_id=reply_to
        )
        if not sent.sent:
            return SendResult(sent.id or None, f"twitch_rejected:{sent.dropped_code}")
        return SendResult(sent.id)

    async def resolve_user(self, login: str) -> dict[str, str] | None:
        login = login.lower().lstrip("@")
        cached = self._users_by_login.get(login)
        if cached is not None:
            self._users_by_login.move_to_end(login)
            return cached
        if self.client is None:
            return None
        users = await self.client.fetch_users(logins=[login], token_for=self.bot_id)
        if not users:
            return None
        return self._remember(users[0].id, users[0].name or login, users[0].display_name or login)

    async def resolve_user_id(self, user_id: str) -> dict[str, str] | None:
        if user_id in self._logins_by_id:
            return self._users_by_login.get(self._logins_by_id[user_id])
        if self.client is None:
            return None
        users = await self.client.fetch_users(ids=[user_id], token_for=self.bot_id)
        if not users:
            return None
        return self._remember(users[0].id, users[0].name or user_id, users[0].display_name or user_id)

    async def login_for(self, user_id: str) -> str | None:
        user = await self.resolve_user_id(user_id)
        return user["name"] if user else None

    def _remember(self, user_id: str, login: str, display: str) -> dict[str, str]:
        record = {"id": user_id, "name": login, "display": display}
        self._users_by_login[login] = record
        self._logins_by_id[user_id] = login
        if len(self._users_by_login) > USER_CACHE_SIZE:
            old_login, old = self._users_by_login.popitem(last=False)
            self._logins_by_id.pop(old["id"], None)
        return record
