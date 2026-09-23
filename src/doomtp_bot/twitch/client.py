"""TwitchService: owns the TwitchIO client — tokens from the `bot` schema, EventSub subscriptions, sending, user lookups.

This is the only module that imports twitchio (ADR-0002).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
import twitchio
from twitchio import eventsub

from doomtp_bot.core import metrics
from doomtp_bot.core.events import Event
from doomtp_bot.core.health import ComponentHealth, Status
from doomtp_bot.core.outbox import BANNED, SendResult
from doomtp_bot.twitch import mapping
from doomtp_bot.twitch.tokens import BOT_IDENTITY, TokenStore

log = structlog.get_logger(__name__)

EventSink = Callable[[Event], Awaitable[None]]
DEDUPE_SIZE = 2000
USER_CACHE_SIZE = 5000
# Why Helix Send a Shoutout said no, in words a moderator can act on.
SHOUTOUT_REFUSALS = {
    400: "Twitch only sends a shoutout while the channel is live",
    401: "the bot's sign-in predates the shoutout permission: sign the bot in again at /auth/login",
    403: "the bot isn't a moderator here",
    429: "Twitch allows one shoutout every 2 minutes, and the same streamer once an hour",
}

CHAT_SUBSCRIPTIONS: tuple[type[Any], ...] = (
    eventsub.ChatMessageSubscription,
    eventsub.ChatNotificationSubscription,
    eventsub.ChatMessageDeleteSubscription,
    eventsub.ChatClearSubscription,
    eventsub.ChatClearUserMessagesSubscription,
)
# Full tier only: these run on the *broadcaster's* token, so they exist only where one was granted
# (ADR-0007 item 5). Each is keyed by the capability its scope buys.
BROADCASTER_SUBSCRIPTIONS: tuple[tuple[str, type[Any]], ...] = (
    ("redemptions", eventsub.ChannelPointsRedeemAddSubscription),
    ("bits", eventsub.ChannelCheerSubscription),
)


def _already_subscribed(exc: Exception) -> bool:
    """TwitchIO raises on a duplicate subscription; that still means the bot is allowed to have it."""
    text = repr(exc).lower()
    return "409" in text or "conflict" in text or "already" in text


def _unauthorized(exc: Exception) -> bool:
    """Twitch refusing the token itself, rather than a request that went wrong on the way."""
    text = repr(exc).lower()
    return "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text


@dataclass(frozen=True, slots=True)
class BroadcasterEvents:
    """What came of subscribing with a broadcaster's token (ADR-0007 item 5)."""

    failed: tuple[str, ...] = ()
    #: Twitch refused the token, so the grant is gone — not a request that failed on the way.
    unauthorized: bool = False


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

    async def event_websocket_welcome(self, payload: Any) -> None:
        # One per EventSub session: a first connection or a reconnect, which TwitchIO doesn't tell apart
        # for us (ADR-0015).
        metrics.EVENTSUB_WELCOMES.inc()
        log.info("twitch.eventsub_welcome", session=payload.id)

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

    async def event_follow(self, payload: Any) -> None:
        await self.service.emit(None, mapping.follow(payload))

    async def event_custom_redemption_add(self, payload: Any) -> None:
        await self.service.emit(payload.id, mapping.redemption(payload))

    async def event_cheer(self, payload: Any) -> None:
        await self.service.emit(None, mapping.cheer(payload))


class TwitchService:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        tokens: TokenStore,
        sink: EventSink,
        on_stopped: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens = tokens
        self.sink = sink
        #: Called when the client stops by itself, so whoever started it can start it again (ADR-0001).
        self.on_stopped = on_stopped
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
        else:
            self.last_error = "the client returned on its own"
            log.warning("twitch.client_stopped", error=self.last_error)
        if self.on_stopped is not None and self.client is client:
            metrics.TWITCH_CLIENT_RESTARTS.inc()
            # Nobody asked for this, so EventSub gave up on its own: hand it back to be started again,
            # from a new task, because starting again cancels this one.
            asyncio.create_task(self.on_stopped(), name="twitch-restart")  # noqa: RUF006

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
        if channel_id in self._subscribed:
            return []
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

    async def use_broadcaster_token(self, access_token: str, refresh_token: str) -> bool:
        """Hand a broadcaster's token to the client so its own events can be subscribed to."""
        if self.client is None:
            return False
        try:
            await self.client.add_token(access_token, refresh_token)
        except Exception as exc:
            log.warning("twitch.broadcaster_token_rejected", error=repr(exc))
            return False
        return True

    async def subscribe_broadcaster(self, channel_id: str, capabilities: set[str]) -> BroadcasterEvents:
        """Subscribe to the full-tier events this channel granted, and say what came of it."""
        wanted = [sub for capability, sub in BROADCASTER_SUBSCRIPTIONS if capability in capabilities]
        if self.client is None:
            return BroadcasterEvents(tuple(sub.type for sub in wanted))
        failed: list[str] = []
        refused = False
        for subscription in wanted:
            try:
                await self.client.subscribe_websocket(
                    subscription(broadcaster_user_id=channel_id), token_for=channel_id
                )
            except Exception as exc:
                if _already_subscribed(exc):
                    continue
                failed.append(subscription.type)
                refused = refused or _unauthorized(exc)
                log.warning(
                    "twitch.broadcaster_subscribe_failed",
                    channel=channel_id,
                    type=subscription.type,
                    error=repr(exc),
                )
        return BroadcasterEvents(tuple(failed), refused)

    async def unsubscribe_channel(self, channel_id: str) -> None:
        self._subscribed.discard(channel_id)
        if self.client is None:
            return
        for sub_id, sub in self.client.websocket_subscriptions().items():
            if sub.condition.get("broadcaster_user_id") == channel_id:
                try:
                    await self.client.delete_websocket_subscription(sub_id, force=True)
                except Exception as exc:
                    log.warning(
                        "twitch.unsubscribe_failed", channel=channel_id, type=sub.type, error=repr(exc)
                    )

    # ── Helix ───────────────────────────────────────────────────────────────
    async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
        if self.client is None or self.bot_id is None:
            return SendResult(None, "not_connected")
        channel = self.client.create_partialuser(channel_id)
        try:
            sent = await channel.send_message(
                text, self.bot_id, token_for=self.bot_id, reply_to_message_id=reply_to
            )
        except twitchio.HTTPException as exc:
            # Send Chat Message answers 403 when "the sender is not permitted to send chat messages to
            # the broadcaster's chat room": a ban. Anything else stays an ordinary failure.
            if exc.status != 403:
                raise
            log.warning("twitch.send_forbidden", channel=channel_id, error=exc.extra.get("message", ""))
            return SendResult(None, BANNED)
        if not sent.sent:
            return SendResult(sent.id or None, f"twitch_rejected:{sent.dropped_code}")
        return SendResult(sent.id)

    async def delete_message(self, channel_id: str, message_id: str) -> bool:
        """Delete one message as the bot. Needs the moderator tier (architecture §9.3)."""
        if self.client is None or self.bot_id is None:
            return False
        try:
            await self.client.create_partialuser(channel_id).delete_chat_messages(
                moderator=self.bot_id, message_id=message_id, token_for=self.bot_id
            )
        except Exception as exc:
            log.warning("twitch.delete_failed", channel=channel_id, error=repr(exc))
            return False
        return True

    async def timeout_user(self, channel_id: str, user_id: str, seconds: int, reason: str) -> bool:
        """Time a chatter out as the bot. The reason is shown to them, so it stays generic."""
        if self.client is None or self.bot_id is None:
            return False
        try:
            await self.client.create_partialuser(channel_id).timeout_user(
                moderator=self.bot_id,
                user=user_id,
                duration=seconds,
                reason=reason,
                token_for=self.bot_id,
            )
        except Exception as exc:
            log.warning("twitch.timeout_failed", channel=channel_id, user=user_id, error=repr(exc))
            return False
        return True

    async def shoutout(self, channel_id: str, to_user_id: str) -> str | None:
        """Twitch's own Shoutout card (Helix Send a Shoutout), as the bot. None if sent, else why not."""
        if self.client is None or self.bot_id is None:
            return "not connected to Twitch"
        try:
            await self.client.create_partialuser(channel_id).send_shoutout(
                to_broadcaster=to_user_id, moderator=self.bot_id, token_for=self.bot_id
            )
        except twitchio.HTTPException as exc:
            log.warning("twitch.shoutout_failed", channel=channel_id, status=exc.status)
            return SHOUTOUT_REFUSALS.get(exc.status, f"Twitch answered {exc.status}")
        return None

    async def last_game(self, user_id: str) -> str | None:
        """What a channel last streamed (Helix Get Channel Information), or None if unknown."""
        if self.client is None:
            return None
        try:
            found = await self.client.fetch_channels([user_id], token_for=self.bot_id)
        except Exception as exc:
            log.warning("twitch.channel_info_failed", user=user_id, error=repr(exc))
            return None
        return (found[0].game_name or None) if found else None

    async def fetch_live(self, channel_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Helix `Get Streams` for up to 100 channels (ADR-0007). Raises if the request fails."""
        if self.client is None:
            raise RuntimeError("twitch client is not connected")
        live: dict[str, dict[str, Any]] = {}
        async for stream in self.client.fetch_streams(user_ids=list(channel_ids), type="live"):
            live[str(stream.user.id)] = {
                "title": stream.title or "",
                "game": stream.game_name or "",
                "viewers": int(stream.viewer_count or 0),
                "started_at": stream.started_at.isoformat() if stream.started_at else "",
            }
        return live

    async def try_moderator_subscription(self, channel_id: str) -> bool:
        """Subscribe to `channel.follow`, which only a moderator may. Success *is* the mod check.

        The subscription is kept: follow triggers need it, and it costs one slot in the channels where
        the bot is a mod (ADR-0007 CapabilityProbe).
        """
        if self.client is None or self.bot_id is None:
            return False
        try:
            await self.client.subscribe_websocket(
                eventsub.ChannelFollowSubscription(
                    broadcaster_user_id=channel_id, moderator_user_id=self.bot_id
                ),
                token_for=self.bot_id,
            )
        except Exception as exc:
            if _already_subscribed(exc):
                return True
            log.debug("twitch.not_moderator", channel=channel_id, error=repr(exc))
            return False
        return True

    async def resolve_user(self, login: str) -> dict[str, str] | None:
        login = login.lower().lstrip("@")
        cached = self._users_by_login.get(login)
        if cached is not None:
            self._users_by_login.move_to_end(login)
            return cached
        return await self._fetch_user(login, logins=[login])

    async def resolve_user_id(self, user_id: str) -> dict[str, str] | None:
        login = self._logins_by_id.get(user_id)
        if login is not None and login in self._users_by_login:
            self._users_by_login.move_to_end(login)
            return self._users_by_login[login]
        return await self._fetch_user(user_id, ids=[user_id])

    async def _fetch_user(self, fallback_name: str, **by: list[str]) -> dict[str, str] | None:
        if self.client is None:
            return None
        users = await self.client.fetch_users(**by, token_for=self.bot_id)  # type: ignore[arg-type]
        if not users:
            return None
        user = users[0]
        return self._remember(user.id, user.name or fallback_name, user.display_name or fallback_name)

    async def login_for(self, user_id: str) -> str | None:
        user = await self.resolve_user_id(user_id)
        return user["name"] if user else None

    def _remember(self, user_id: str, login: str, display: str) -> dict[str, str]:
        record = {"id": user_id, "name": login, "display": display}
        self._users_by_login[login] = record
        self._logins_by_id[user_id] = login
        if len(self._users_by_login) > USER_CACHE_SIZE:
            _, old = self._users_by_login.popitem(last=False)
            self._logins_by_id.pop(old["id"], None)
        return record
