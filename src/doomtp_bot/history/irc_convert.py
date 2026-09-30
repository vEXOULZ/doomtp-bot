"""What an IRC line says, in EventSub's terms (ADR-0024 §2).

The fields a backfilled line fills in the chat log, converted the way EventSub would have sent them:
fragments rebuilt from the `emotes` tag, `msg-id` as a message or notice type, and a notice's
`msg-param-*` as its details. What IRC doesn't carry (an emote's owner, a mention's user id, a
cheermote's tier) is left for enrichment (ADR-0024 §3).
"""

from __future__ import annotations

import re
from typing import Any

from doomtp_bot.history.irc_parse import IrcLine

ACTION_START, ACTION_END = "\x01ACTION ", "\x01"

# PRIVMSG `msg-id` → EventSub `message_type`; a line without one is plain text.
MESSAGE_TYPES = {
    "highlighted-message": "channel_points_highlighted",
    "skip-subs-mode-message": "channel_points_sub_only",
    "user-intro": "user_intro",
    "animated-message": "power_ups_message_effect",
    "gigantified-emote-message": "power_ups_gigantified_emote",
}

# USERNOTICE `msg-id` → EventSub `notice_type`; any other keeps its IRC name.
NOTICE_TYPES = {
    "subgift": "sub_gift",
    "submysterygift": "community_sub_gift",
    "giftpaidupgrade": "gift_paid_upgrade",
    "anongiftpaidupgrade": "gift_paid_upgrade",
    "primepaidupgrade": "prime_paid_upgrade",
    "standardpayforward": "pay_it_forward",
    "communitypayforward": "pay_it_forward",
    "bitsbadgetier": "bits_badge_tier",
    "charitydonation": "charity_donation",
    "viewermilestone": "watch_streak",
}

_WORD_START = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Twitch's stand-in for a gifter who chose to stay anonymous; EventSub names no one.
ANONYMOUS_GIFTER_ID = "274598607"


def text(line: IrcLine) -> str:
    """The message text, without the `/me` wrapper IRC puts around it (EventSub sends the text alone)."""
    body = line.text
    if body.startswith(ACTION_START) and body.endswith(ACTION_END):
        return body[len(ACTION_START) : -len(ACTION_END)]
    return body


def fragments(line: IrcLine) -> tuple[dict[str, Any], ...]:
    """Text and emote fragments, as `twitch/mapping.py` stores EventSub's.

    `emotes=25:0-4,12-16/1902:6-10` gives each emote's id and its positions in the text, counted in code
    points and inclusive at both ends. Mentions and cheermotes stay text: telling them apart needs lookups
    (ADR-0024 §3).
    """
    body = text(line)
    spans: list[tuple[int, int, str]] = []
    for entry in line.tag("emotes").split("/"):
        emote_id, _, positions = entry.partition(":")
        for span in positions.split(","):
            first, _, last = span.partition("-")
            if emote_id and first.isdigit() and last.isdigit():
                spans.append((int(first), int(last) + 1, emote_id))
    found: list[dict[str, Any]] = []
    at = 0
    for start, end, emote_id in sorted(spans):
        if start < at or end > len(body):
            continue  # overlapping or out of range: the tag doesn't fit this text, keep it as text
        if start > at:
            found.append({"type": "text", "text": body[at:start]})
        found.append({"type": "emote", "text": body[start:end], "emote_id": emote_id})
        at = end
    if at < len(body) or not found:
        found.append({"type": "text", "text": body[at:]})
    return tuple(found)


def message_type(line: IrcLine) -> str:
    return MESSAGE_TYPES.get(line.tag("msg-id"), "text")


def badge_info(line: IrcLine) -> dict[str, str]:
    """`badge-info=subscriber/14` → {"subscriber": "14"}: the months behind a sub badge."""
    info: dict[str, str] = {}
    for item in line.tag("badge-info").split(","):
        set_id, _, value = item.partition("/")
        if set_id:
            info[set_id] = value
    return info


def notice_type(line: IrcLine) -> str:
    """A USERNOTICE's EventSub `notice_type`. Shared chat names the original notice in `source-msg-id`."""
    msg_id = line.tag("msg-id") or "usernotice"
    if msg_id == "sharedchatnotice":
        original = line.tag("source-msg-id")
        return f"shared_chat_{NOTICE_TYPES.get(original, original)}" if original else msg_id
    return NOTICE_TYPES.get(msg_id, msg_id)


def notice_user_id(line: IrcLine) -> str | None:
    user_id = line.tag("user-id")
    return None if not user_id or user_id == ANONYMOUS_GIFTER_ID else user_id


def notice_payload(line: IrcLine) -> dict[str, Any]:
    """The payload `twitch/mapping.py` stores for a live notice, from the line's tags.

    `detail` holds the `msg-param-*` tags in snake case without the prefix, which is how EventSub names
    most of them (`msg-param-cumulative-months` → `cumulative_months`, `msg-param-viewerCount` →
    `viewer_count`).
    """
    user_id = notice_user_id(line)
    return {
        "system_message": line.tag("system-msg"),
        "text": text(line),
        "chatter": None if user_id is None else {"id": user_id, "login": line.tag("login") or line.nick},
        "detail": {
            _WORD_START.sub("_", key.removeprefix("msg-param-")).replace("-", "_").lower(): value
            for key, value in line.tags.items()
            if key.startswith("msg-param-")
        },
    }
