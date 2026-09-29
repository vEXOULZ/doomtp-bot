"""EventSub-shaped objects for events that arrive with neither an EventSub event nor an IRC line (ADR-0024).

A live event carries its `event` JSON and a backfilled one its IRC line; either is stored as `raw` as it
came. Anything else — a test's event, or `ModerationAction`, which nothing receives from Twitch yet — is
stored as `legacy`: the same object chatlog migration 0002 rebuilt from the columns of rows logged
before it. The two must agree (`tests/test_raw_events.py`), since a reader cannot tell them apart.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from doomtp_bot.core.events import ChatMessage, ChatNotification


def _strip(value: Any) -> Any:
    """Leave out what was never known, as `jsonb_strip_nulls` does: null object members, at any depth."""
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _known(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: _strip(v) for k, v in fields.items() if v is not None}


def _fragment(fragment: dict[str, Any]) -> dict[str, Any]:
    """`twitch.mapping._fragment`'s shape back to Twitch's."""
    mention = fragment.get("mention")
    cheermote = fragment.get("cheermote")
    return {
        "type": fragment.get("type"),
        "text": fragment.get("text"),
        "cheermote": None
        if cheermote is None
        else {"prefix": cheermote.get("prefix"), "bits": cheermote.get("bits")},
        "emote": {"id": fragment["emote_id"]} if "emote_id" in fragment else None,
        "mention": None
        if mention is None
        else {"user_id": mention.get("id"), "user_login": mention.get("login")},
    }


def message(msg: ChatMessage) -> dict[str, Any]:
    return _known(
        {
            "broadcaster_user_id": msg.channel_id,
            "chatter_user_id": msg.user_id,
            "chatter_user_login": msg.user_login,
            "chatter_user_name": msg.display_name,
            "message_id": msg.message_id,
            "message": {"text": msg.text, "fragments": [_fragment(f) for f in msg.fragments]},
            "message_type": msg.message_type,
            "badges": [asdict(b) for b in msg.badges],
            "cheer": {"bits": msg.bits} if msg.bits > 0 else None,
            "reply": None if msg.reply_parent_id is None else {"parent_message_id": msg.reply_parent_id},
            "channel_points_custom_reward_id": msg.reward_id,
            "source_broadcaster_user_id": msg.source_channel_id,
        }
    )


def notification(n: ChatNotification) -> dict[str, Any]:
    known = _known({"broadcaster_user_id": n.channel_id, "chatter_user_id": n.user_id, "notice_type": n.type})
    return known | {"legacy": n.payload}  # the payload as it was, nulls and all


def mod_event(
    channel_id: str,
    *,
    message_id: str | None,
    target: str | None,
    moderator: str | None,
    duration_s: int | None,
    reason: str | None,
) -> dict[str, Any]:
    return _known(
        {
            "broadcaster_user_id": channel_id,
            "message_id": message_id,
            "target_user_id": target,
            "moderator_user_id": moderator,
            "duration_s": duration_s,
            "reason": reason,
        }
    )
