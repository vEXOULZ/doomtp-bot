"""Domain events published on the EventBus. Only twitch/ and history/ construct these from external payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Source = Literal["eventsub", "recent-messages"]


@dataclass(frozen=True, slots=True)
class Badge:
    set_id: str
    id: str
    info: str = ""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    message_id: str
    channel_id: str
    channel_login: str
    user_id: str
    user_login: str
    display_name: str
    text: str
    sent_at: int  # ms epoch (Twitch)
    received_at: int  # ms epoch (ours)
    badges: tuple[Badge, ...] = ()
    fragments: tuple[dict[str, Any], ...] = ()
    message_type: str = "text"
    bits: int = 0
    reply_parent_id: str | None = None
    reply_parent_login: str | None = None
    reply_parent_display: str | None = None
    reward_id: str | None = None
    source_channel_id: str | None = None
    is_self: bool = False
    source: Source = "eventsub"
    raw: str | None = None  # original IRC line when backfilled

    @property
    def reply_mentions(self) -> tuple[str, ...]:
        """Names Twitch may have prefixed as `@name` on a reply (display name first, as observed live)."""
        return tuple(n for n in (self.reply_parent_display, self.reply_parent_login) if n)


@dataclass(frozen=True, slots=True)
class ChatNotification:
    id: str
    channel_id: str
    user_id: str | None
    type: str  # sub | resub | sub_gift | raid | announcement | ...
    payload: dict[str, Any]
    sent_at: int
    source: Source = "eventsub"


@dataclass(frozen=True, slots=True)
class MessageDeleted:
    channel_id: str
    message_id: str
    target_user_id: str
    at: int
    source: Source = "eventsub"


@dataclass(frozen=True, slots=True)
class UserMessagesCleared:
    """A timeout or ban happened (basic tier cannot tell which, nor who did it)."""

    channel_id: str
    target_user_id: str
    at: int
    source: Source = "eventsub"


@dataclass(frozen=True, slots=True)
class ChatCleared:
    channel_id: str
    at: int
    source: Source = "eventsub"


@dataclass(frozen=True, slots=True)
class ModerationAction:
    """Moderator/full tier enrichment from channel.moderate."""

    channel_id: str
    action: str
    moderator_user_id: str
    target_user_id: str | None
    at: int
    duration_s: int | None = None
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StreamStatusChanged:
    channel_id: str
    live: bool
    at: int


Event = (
    ChatMessage
    | ChatNotification
    | MessageDeleted
    | UserMessagesCleared
    | ChatCleared
    | ModerationAction
    | StreamStatusChanged
)
