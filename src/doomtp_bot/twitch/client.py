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
# Why any other Helix action was refused, by whose token it went out on. A status not listed here takes
# Twitch's own message, which for a 400 names the problem ("The user is already banned").
BOT_REFUSALS = {
    401: "the bot's sign-in predates this permission: sign the bot in again at /auth/login",
    403: "the bot isn't a moderator here",
    429: "Twitch is rate limiting the bot: try again in a moment",
}
BROADCASTER_REFUSALS = {
    401: "the broadcaster hasn't granted this: they can connect the channel again at /auth/connect",
    403: "the broadcaster hasn't granted this: they can connect the channel again at /auth/connect",
    429: "Twitch is rate limiting the channel: try again in a moment",
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


def _refusal(exc: twitchio.HTTPException, refusals: dict[int, str]) -> str:
    if exc.status in refusals:
        return refusals[exc.status]
    message = exc.extra.get("message") if isinstance(exc.extra, dict) else None
    return f"Twitch said: {message}" if message else f"Twitch answered {exc.status}"


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
        expected_bot_id: str | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens = tokens
        self.sink = sink
        #: Called when the client stops by itself, so whoever started it can start it again (ADR-0001).
        self.on_stopped = on_stopped
        #: TWITCH_BOT_ID. A stored token for any other account is refused rather than run with.
        self.expected_bot_id = expected_bot_id
        #: Why start() refused the stored token, for health; None once a start gets past that check.
        self.refused: str | None = None
        self.client: _BotClient | None = None
        self.bot_id: str | None = None
        self.bot_login: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._subscribed: set[str] = set()
        self._pinned: dict[str, str] = {}  # channel → the message the bot last pinned there
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
        if self.expected_bot_id and stored.user_id != self.expected_bot_id:
            # /auth/callback checks the account, but a token stored before TWITCH_BOT_ID changed would
            # otherwise go on running as the old account, into the old account's channels.
            self.refused = (
                f"the stored bot token is for {stored.login} ({stored.user_id}), but TWITCH_BOT_ID is"
                f" {self.expected_bot_id}; open /auth/login and sign in as the bot account"
            )
            log.error(
                "twitch.wrong_bot_account",
                stored=stored.login,
                stored_id=stored.user_id,
                expected_id=self.expected_bot_id,
            )
            await self.stop()
            return False
        self.refused = None
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
            reason = self.refused or "bot not authorized; open /auth/login"
            return ComponentHealth(Status.DEGRADED, {"reason": reason})
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

    # Moderator and channel actions (ADR-0019 item 3). Each returns None when Twitch did it, else why not.
    async def _act(
        self, what: str, call: Callable[[Any], Awaitable[Any]], channel_id: str, *, as_bot: bool = True
    ) -> str | None:
        if self.client is None or self.bot_id is None:
            return "not connected to Twitch"
        try:
            await call(self.client.create_partialuser(channel_id))
        except twitchio.HTTPException as exc:
            log.warning("twitch.action_refused", action=what, channel=channel_id, status=exc.status)
            return _refusal(exc, BOT_REFUSALS if as_bot else BROADCASTER_REFUSALS)
        except Exception as exc:
            # No token for the broadcaster, most likely: the channel was never connected.
            log.warning("twitch.action_failed", action=what, channel=channel_id, error=repr(exc))
            return "Twitch didn't take it" + (
                "" if as_bot else ": is the channel connected at /auth/connect?"
            )
        return None

    async def ban_user(self, channel_id: str, user_id: str, reason: str) -> str | None:
        return await self._act(
            "ban",
            lambda c: c.ban_user(
                moderator=self.bot_id, user=user_id, reason=reason or None, token_for=self.bot_id
            ),
            channel_id,
        )

    async def unban_user(self, channel_id: str, user_id: str) -> str | None:
        """Lifts a ban or a timeout: Twitch's Unban does both."""
        return await self._act(
            "unban",
            lambda c: c.unban_user(moderator=self.bot_id, user_id=user_id, token_for=self.bot_id),
            channel_id,
        )

    async def warn_user(self, channel_id: str, user_id: str, reason: str) -> str | None:
        return await self._act(
            "warn",
            lambda c: c.warn_user(
                moderator=self.bot_id, user_id=user_id, reason=reason, token_for=self.bot_id
            ),
            channel_id,
        )

    async def announce(self, channel_id: str, text: str, color: str | None) -> str | None:
        return await self._act(
            "announce",
            lambda c: c.send_announcement(
                moderator=self.bot_id, message=text, color=color, token_for=self.bot_id
            ),
            channel_id,
        )

    async def update_chat_settings(self, channel_id: str, settings: dict[str, Any]) -> str | None:
        return await self._act(
            "chat_settings",
            lambda c: c.update_chat_settings(self.bot_id, token_for=self.bot_id, **settings),
            channel_id,
        )

    async def clear_chat(self, channel_id: str) -> str | None:
        return await self._act(
            "clear",
            lambda c: c.delete_chat_messages(moderator=self.bot_id, token_for=self.bot_id),
            channel_id,
        )

    async def shield_mode(self, channel_id: str, active: bool) -> str | None:
        return await self._act(
            "shield",
            lambda c: c.update_shield_mode_status(
                moderator=self.bot_id, active=active, token_for=self.bot_id
            ),
            channel_id,
        )

    async def pin_message(self, channel_id: str, message_id: str, duration: int | None) -> str | None:
        """Helix Pin Chat Message; `duration` in seconds (30–1800), None until unpinned."""
        refused = await self._act(
            "pin",
            lambda c: c.pin_message(
                message_id=message_id, moderator=self.bot_id, duration=duration, token_for=self.bot_id
            ),
            channel_id,
        )
        if refused is None:
            self._pinned[channel_id] = message_id
        return refused

    async def unpin_message(self, channel_id: str, message_id: str | None) -> str | None:
        """Unpin that message, or else the last one the bot pinned in the channel."""
        message_id = message_id or self._pinned.get(channel_id)
        if message_id is None:
            return "reply to the pinned message to unpin it"
        refused = await self._act(
            "unpin",
            lambda c: c.unpin_message(message_id=message_id, moderator=self.bot_id, token_for=self.bot_id),
            channel_id,
        )
        if refused is None and self._pinned.get(channel_id) == message_id:
            del self._pinned[channel_id]
        return refused

    async def find_game(self, name: str) -> dict[str, str] | None:
        """A category by its exact name, or else Twitch's best search match."""
        if self.client is None:
            return None
        game = await self.client.fetch_game(name=name)
        if game is None:
            async for found in self.client.search_categories(name, max_results=1):
                game = found
                break
        return None if game is None else {"id": str(game.id), "name": game.name}

    # These go out on the broadcaster's own token (ADR-0007 item 5): no moderator may do them for Twitch.
    async def update_channel(
        self, channel_id: str, *, title: str | None = None, game_id: str | None = None
    ) -> str | None:
        return await self._act(
            "modify_channel",
            lambda c: c.modify_channel(title=title, game_id=game_id),
            channel_id,
            as_bot=False,
        )

    async def stream_marker(self, channel_id: str, description: str | None) -> str | None:
        return await self._act(
            "marker",
            lambda c: c.create_stream_marker(token_for=channel_id, description=description),
            channel_id,
            as_bot=False,
        )

    async def start_raid(self, channel_id: str, to_user_id: str) -> str | None:
        return await self._act(
            "raid", lambda c: c.start_raid(to_broadcaster=to_user_id), channel_id, as_bot=False
        )

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

    async def fetch_schedule(self, channel_id: str) -> list[dict[str, str]]:
        """The channel's next scheduled streams from Helix `Get Channel Stream Schedule` (app token, no
        scope), soonest first, as {title, category, start}. Cancelled segments and those inside a vacation
        are left out, and a channel with no schedule has none. Raises if the request fails."""
        if self.client is None:
            raise RuntimeError("twitch client is not connected")
        upcoming: list[dict[str, str]] = []
        try:
            async for schedule in self.client.create_partialuser(channel_id).fetch_stream_schedule(
                first=5, max_results=1
            ):
                away = schedule.vacation
                for segment in schedule.segments:
                    if segment.canceled_until is not None:
                        continue
                    if away is not None and away.start_time <= segment.start_time < away.end_time:
                        continue
                    upcoming.append(
                        {
                            "title": segment.title or "",
                            "category": segment.category.name if segment.category else "",
                            "start": segment.start_time.isoformat(),
                        }
                    )
        except twitchio.HTTPException as exc:
            if exc.status == 404:  # Twitch's answer for a channel that never set a schedule
                return []
            raise
        except (TypeError, IndexError):  # twitchio can't read a page whose `segments` is null or empty
            return []
        return sorted(upcoming, key=lambda s: s["start"])

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
