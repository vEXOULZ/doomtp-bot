"""Map TwitchIO EventSub payloads to domain events. Pure functions: attribute access only, easy to test with fakes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from doomtp_bot.clock import now_ms
from doomtp_bot.core.events import (
    Badge,
    ChatCleared,
    ChatMessage,
    ChatNotification,
    MessageDeleted,
    UserMessagesCleared,
)


def _ms(moment: datetime | None) -> int:
    return int(moment.timestamp() * 1000) if moment is not None else now_ms()


def _fragment(fragment: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"type": fragment.type, "text": fragment.text}
    if getattr(fragment, "mention", None) is not None:
        data["mention"] = {"id": fragment.mention.id, "login": fragment.mention.name}
    if getattr(fragment, "emote", None) is not None:
        data["emote_id"] = fragment.emote.id
    if getattr(fragment, "cheermote", None) is not None:
        data["cheermote"] = {"prefix": fragment.cheermote.prefix, "bits": fragment.cheermote.bits}
    return data


def chat_message(payload: Any, bot_id: str | None) -> ChatMessage:
    """twitchio.ChatMessage → ChatMessage."""
    chatter = payload.chatter
    reply = payload.reply
    source = getattr(payload, "source_broadcaster", None)
    cheer = getattr(payload, "cheer", None)
    return ChatMessage(
        message_id=payload.id,
        channel_id=payload.broadcaster.id,
        channel_login=payload.broadcaster.name or "",
        user_id=chatter.id,
        user_login=chatter.name or "",
        display_name=chatter.display_name or chatter.name or "",
        text=payload.text,
        sent_at=_ms(payload.timestamp),
        received_at=now_ms(),
        badges=tuple(Badge(b.set_id, b.id, b.info or "") for b in payload.badges),
        fragments=tuple(_fragment(f) for f in payload.fragments),
        message_type=payload.type,
        bits=cheer.bits if cheer is not None else 0,
        reply_parent_id=reply.parent_message_id if reply is not None else None,
        reply_parent_login=reply.parent_user.name if reply is not None else None,
        reply_parent_display=reply.parent_user.display_name if reply is not None else None,
        reward_id=payload.channel_points_id,
        source_channel_id=source.id if source is not None else None,
        is_self=bot_id is not None and chatter.id == bot_id,
    )


NOTIFICATION_FIELDS = (
    "sub", "resub", "sub_gift", "community_sub_gift", "gift_paid_upgrade", "prime_paid_upgrade", "raid",
    "pay_it_forward", "announcement", "bits_badge_tier", "charity_donation", "watch_streak", "modiversary",
)  # fmt: skip


def _plain(value: Any, depth: int = 0) -> Any:
    """Best-effort conversion of TwitchIO model objects to JSON-able data (slots, PartialUser)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth > 3:
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v, depth + 1) for k, v in value.items()}
    if hasattr(value, "id") and hasattr(value, "name") and hasattr(value, "display_name"):
        return {"id": value.id, "login": value.name, "display": value.display_name}
    slots = [s for cls in type(value).__mro__ for s in getattr(cls, "__slots__", ())]
    names = slots or list(getattr(value, "__dict__", {}))
    if names:
        return {n: _plain(getattr(value, n, None), depth + 1) for n in names if not n.startswith("_")}
    return str(value)


def follow(payload: Any) -> ChatNotification:
    """twitchio.ChannelFollow → ChatNotification(type="follow"). Moderator tier only (ADR-0007)."""
    user = payload.user
    at = _ms(getattr(payload, "followed_at", None))
    return ChatNotification(
        id=f"follow:{user.id}:{at}",
        channel_id=payload.broadcaster.id,
        user_id=user.id,
        type="follow",
        payload={
            "system_message": f"{user.display_name or user.name} followed",
            "text": "",
            "user": {"id": user.id, "name": user.name, "display": user.display_name or user.name},
            "followed_at": at,
        },
        sent_at=at,
    )


def redemption(payload: Any) -> ChatNotification:
    """twitchio.ChannelPointsRedemptionAdd → ChatNotification(type="redemption"). Full tier (ADR-0007)."""
    user, reward = payload.user, payload.reward
    at = _ms(getattr(payload, "redeemed_at", None))
    text = payload.user_input or ""
    return ChatNotification(
        id=f"redemption:{payload.id}",
        channel_id=payload.broadcaster.id,
        user_id=user.id,
        type="redemption",
        payload={
            "system_message": f"{user.display_name or user.name} redeemed {reward.title}",
            "text": text,
            "input": text,  # {event.input} is the redemption's own text (architecture §7)
            "user": {"id": user.id, "name": user.name, "display": user.display_name or user.name},
            "reward": {"id": reward.id, "title": reward.title, "cost": reward.cost},
            "status": getattr(payload, "status", ""),
        },
        sent_at=at,
    )


def cheer(payload: Any) -> ChatNotification:
    """twitchio.ChannelCheer → ChatNotification(type="cheer"). Full tier (ADR-0007)."""
    anonymous = bool(getattr(payload, "anonymous", False))
    user = None if anonymous else payload.user
    at = _ms(getattr(payload, "timestamp", None))
    bits = int(getattr(payload, "bits", 0) or 0)
    who = "someone" if user is None else (user.display_name or user.name)
    return ChatNotification(
        id=f"cheer:{payload.broadcaster.id}:{at}:{bits}",
        channel_id=payload.broadcaster.id,
        user_id=None if user is None else user.id,
        type="cheer",
        payload={
            "system_message": f"{who} cheered {bits} bits",
            "text": payload.message or "",
            "bits": bits,
            "anonymous": anonymous,
            "user": None
            if user is None
            else {"id": user.id, "name": user.name, "display": user.display_name or user.name},
        },
        sent_at=at,
    )


def chat_notification(payload: Any) -> ChatNotification:
    """twitchio.ChatNotification → ChatNotification (payload keeps the notice-specific details)."""
    notice = payload.notice_type
    detail = getattr(payload, notice, None) if notice in NOTIFICATION_FIELDS else None
    return ChatNotification(
        id=payload.id,
        channel_id=payload.broadcaster.id,
        user_id=None if payload.anonymous else payload.chatter.id,
        type=notice,
        payload={
            "system_message": payload.system_message,
            "text": payload.text,
            "chatter": None
            if payload.anonymous
            else {"id": payload.chatter.id, "login": payload.chatter.name},
            "detail": _plain(detail),
        },
        sent_at=_ms(payload.timestamp),
    )


def message_deleted(payload: Any) -> MessageDeleted:
    return MessageDeleted(
        channel_id=payload.broadcaster.id,
        message_id=payload.message_id,
        target_user_id=payload.user.id,
        at=_ms(payload.timestamp),
    )


def user_messages_cleared(payload: Any) -> UserMessagesCleared:
    return UserMessagesCleared(
        channel_id=payload.broadcaster.id, target_user_id=payload.user.id, at=_ms(payload.timestamp)
    )


def chat_cleared(payload: Any) -> ChatCleared:
    return ChatCleared(channel_id=payload.broadcaster.id, at=_ms(payload.timestamp))
