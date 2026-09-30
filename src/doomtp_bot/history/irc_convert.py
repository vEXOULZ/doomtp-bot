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
        "detail": {_snake(key): value for key, value in line.tags.items() if key.startswith("msg-param-")},
    }


def _snake(tag: str) -> str:
    """`msg-param-viewerCount` → `viewer_count`."""
    return _WORD_START.sub("_", tag.removeprefix("msg-param-")).replace("-", "_").lower()


# ── whole events (ADR-0024 §2) ─────────────────────────────────────────────
# Tags EventSub has no field for, kept under the event's `irc` key rather than dropped.
MESSAGE_ONLY_TAGS = (
    "first-msg", "returning-chatter", "flags", "client-nonce", "mod", "subscriber", "turbo", "vip",
    "user-type", "emote-only", "pinned-chat-paid-amount", "pinned-chat-paid-currency",
    "pinned-chat-paid-exponent", "pinned-chat-paid-level", "pinned-chat-paid-is-system-message",
)  # fmt: skip


def _twitch_fragment(fragment: dict[str, Any]) -> dict[str, Any]:
    """Our fragment shape (`fragments`) as Twitch sends it, with what IRC doesn't know as null."""
    return {
        "type": fragment["type"],
        "text": fragment["text"],
        "cheermote": None,
        "emote": {"id": fragment["emote_id"]} if "emote_id" in fragment else None,
        "mention": None,
    }


def _badges(line: IrcLine, name: str = "badges", info_name: str = "badge-info") -> list[dict[str, str]]:
    info: dict[str, str] = {}
    for item in line.tag(info_name).split(","):
        set_id, _, value = item.partition("/")
        if set_id:
            info[set_id] = value
    found = []
    for item in line.tag(name).split(","):
        set_id, _, version = item.partition("/")
        if set_id:
            found.append({"set_id": set_id, "id": version, "info": info.get(set_id, "")})
    return found


def _or_none(value: str) -> str | None:
    return value or None


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value and value.lstrip("-").isdigit() else None


def _irc_only(line: IrcLine, names: tuple[str, ...]) -> dict[str, str]:
    return {name: line.tags[name] for name in names if name in line.tags}


def _chatter(line: IrcLine) -> dict[str, Any]:
    login = line.tag("login") or line.nick
    return {
        "chatter_user_id": line.tag("user-id"),
        "chatter_user_login": login,
        "chatter_user_name": line.tag("display-name") or login,
    }


def _broadcaster(line: IrcLine) -> dict[str, Any]:
    # IRC names the channel by login only; its display name isn't in the line.
    return {
        "broadcaster_user_id": line.tag("room-id"),
        "broadcaster_user_login": line.channel,
        "broadcaster_user_name": None,
    }


def _source(line: IrcLine) -> dict[str, Any]:
    """Shared chat: the channel the message was first sent in, when it isn't this one."""
    shared = "source-room-id" in line.tags
    return {
        "source_broadcaster_user_id": _or_none(line.tag("source-room-id")),
        "source_message_id": _or_none(line.tag("source-id")),
        "source_badges": _badges(line, "source-badges", "source-badge-info") if shared else None,
    }


def _reply(line: IrcLine) -> dict[str, Any] | None:
    if not line.tag("reply-parent-msg-id"):
        return None
    parent = {
        "message_id": line.tag("reply-parent-msg-id"),
        "user_id": line.tag("reply-parent-user-id"),
        "user_login": line.tag("reply-parent-user-login"),
        "user_name": line.tag("reply-parent-display-name"),
    }
    thread = {
        "message_id": line.tag("reply-thread-parent-msg-id"),
        "user_id": line.tag("reply-thread-parent-user-id"),
        "user_login": line.tag("reply-thread-parent-user-login"),
        "user_name": line.tag("reply-thread-parent-display-name"),
    }
    return {
        **{f"parent_{key}": value for key, value in parent.items()},
        "parent_message_body": line.tag("reply-parent-msg-body"),
        # A reply to a message that starts a thread: the thread is the parent's.
        **{f"thread_{key}": thread[key] or parent[key] for key in parent},
    }


def message_event(line: IrcLine) -> dict[str, Any]:
    """A PRIVMSG as a `channel.chat.message` event. What IRC lacks is null; enrichment fills some of it."""
    bits = line.tag_int("bits")
    return {
        **_broadcaster(line),
        **_chatter(line),
        "message_id": line.tag("id"),
        "message": {"text": text(line), "fragments": [_twitch_fragment(f) for f in fragments(line)]},
        "color": line.tag("color"),
        "badges": _badges(line),
        "message_type": message_type(line),
        "cheer": {"bits": bits} if bits > 0 else None,
        "reply": _reply(line),
        "channel_points_custom_reward_id": _or_none(line.tag("custom-reward-id")),
        **_source(line),
        "irc": _irc_only(line, MESSAGE_ONLY_TAGS),
    }


def _params(line: IrcLine) -> dict[str, str]:
    """`msg-param-*` tags, snake case, without the prefix."""
    return {_snake(key): value for key, value in line.tags.items() if key.startswith("msg-param-")}


def _tier(plan: str | None) -> tuple[str | None, bool]:
    """`sub-plan` → EventSub's (`sub_tier`, `is_prime`): Prime is a tier 1 sub."""
    return ("1000", True) if plan == "Prime" else (plan, False)


def _months(p: dict[str, str], name: str) -> int:
    return _int_or_none(p.pop(name, None)) or 1


def _sub(p: dict[str, str]) -> dict[str, Any]:
    tier, prime = _tier(p.pop("sub_plan", None))
    return {"sub_tier": tier, "is_prime": prime, "duration_months": _months(p, "multimonth_duration")}


def _resub(p: dict[str, str]) -> dict[str, Any]:
    tier, prime = _tier(p.pop("sub_plan", None))
    shared = p.pop("should_share_streak", "0") == "1"
    streak = _int_or_none(p.pop("streak_months", None))
    gifted = p.pop("was_gifted", "false") == "true"
    anonymous = p.pop("anon_gift", "false") == "true"
    return {
        "cumulative_months": _int_or_none(p.pop("cumulative_months", None)),
        "duration_months": _months(p, "multimonth_duration"),
        "streak_months": streak if shared else None,
        "sub_tier": tier,
        "is_prime": prime,
        "is_gift": gifted,
        "gifter_is_anonymous": anonymous if gifted else None,
        "gifter_user_id": p.pop("gifter_id", None),
        "gifter_user_login": p.pop("gifter_login", None),
        "gifter_user_name": p.pop("gifter_name", None),
    }


def _sub_gift(p: dict[str, str]) -> dict[str, Any]:
    return {
        "duration_months": _months(p, "gift_months"),
        # 0 when the gifter keeps their total to themselves; EventSub says null.
        "cumulative_total": _int_or_none(p.pop("sender_count", None)) or None,
        "recipient_user_id": p.pop("recipient_id", None),
        "recipient_user_login": p.pop("recipient_user_name", None),
        "recipient_user_name": p.pop("recipient_display_name", None),
        "sub_tier": p.pop("sub_plan", None),
        "community_gift_id": p.pop("community_gift_id", None),
    }


def _community_sub_gift(p: dict[str, str]) -> dict[str, Any]:
    return {
        "id": p.pop("community_gift_id", None) or p.get("origin_id"),
        "total": _int_or_none(p.pop("mass_gift_count", None)),
        "sub_tier": p.pop("sub_plan", None),
        "cumulative_total": _int_or_none(p.pop("sender_count", None)) or None,
    }


def _gifter(p: dict[str, str], prefix: str, anonymous: bool) -> dict[str, Any]:
    user_id = p.pop(f"{prefix}_id", None)
    login = p.pop(f"{prefix}_login", None) or p.pop(f"{prefix}_user_name", None)
    name = p.pop(f"{prefix}_name", None) or p.pop(f"{prefix}_display_name", None)
    return {
        "gifter_is_anonymous": anonymous,
        "gifter_user_id": None if anonymous else user_id,
        "gifter_user_login": None if anonymous else login,
        "gifter_user_name": None if anonymous else name,
    }


def _raid(p: dict[str, str], line: IrcLine) -> dict[str, Any]:
    return {
        "user_id": line.tag("user-id"),
        "user_login": p.pop("login", None),
        "user_name": p.pop("display_name", None),
        "viewer_count": _int_or_none(p.pop("viewer_count", None)),
        "profile_image_url": p.pop("profile_image_url", None),
    }


def _charity(p: dict[str, str]) -> dict[str, Any]:
    return {
        "charity_name": p.pop("charity_name", None),
        "amount": {
            "value": _int_or_none(p.pop("donation_amount", None)),
            "decimal_places": _int_or_none(p.pop("exponent", None)),
            "currency": p.pop("donation_currency", None),
        },
    }


def _detail(kind: str, msg_id: str, p: dict[str, str], line: IrcLine) -> dict[str, Any]:
    """The notice's own object, by EventSub's field names; what's left of `p` is IRC's alone."""
    match kind:
        case "sub":
            return _sub(p)
        case "resub":
            return _resub(p)
        case "sub_gift":
            return _sub_gift(p)
        case "community_sub_gift":
            return _community_sub_gift(p)
        case "gift_paid_upgrade":
            return _gifter(p, "sender", msg_id == "anongiftpaidupgrade")
        case "prime_paid_upgrade":
            return {"sub_tier": p.pop("sub_plan", None)}
        case "raid":
            return _raid(p, line)
        case "pay_it_forward":
            return _gifter(p, "prior_gifter", p.pop("prior_gifter_anonymous", "false") == "true")
        case "announcement":
            return {"color": p.pop("color", None)}
        case "bits_badge_tier":
            return {"tier": _int_or_none(p.pop("threshold", None))}
        case "charity_donation":
            return _charity(p)
    detail = dict(p)  # nothing in EventSub to rename to: every param is the detail
    p.clear()
    return detail


def notification_event(line: IrcLine) -> dict[str, Any]:
    """A USERNOTICE as a `channel.chat.notification` event, its details under the notice type's key.

    The `msg-param-*` tags EventSub has no field for stay under `irc`, by their IRC names.
    """
    kind = notice_type(line)
    shared = kind.startswith("shared_chat_")
    msg_id = line.tag("source-msg-id") if shared else line.tag("msg-id")
    params = _params(line)
    detail = _detail(kind.removeprefix("shared_chat_"), msg_id, params, line)
    anonymous = notice_user_id(line) is None
    body = text(line)
    irc = _irc_only(line, MESSAGE_ONLY_TAGS)
    # What the detail didn't use, by its IRC name.
    irc.update(
        {
            key: value
            for key, value in line.tags.items()
            if key.startswith("msg-param-") and _snake(key) in params
        }
    )
    nobody = {"chatter_user_id": None, "chatter_user_login": None, "chatter_user_name": None}
    return {
        **_broadcaster(line),
        **(nobody if anonymous else _chatter(line)),
        "chatter_is_anonymous": anonymous,
        "color": line.tag("color"),
        "badges": _badges(line),
        "system_message": line.tag("system-msg"),
        "message_id": line.tag("id"),
        "message": {
            "text": body,
            "fragments": [_twitch_fragment(f) for f in fragments(line)] if body else [],
        },
        "notice_type": kind,
        kind: detail,
        **_source(line),
        "irc": irc,
    }


def moderation_event(line: IrcLine) -> dict[str, Any]:
    """CLEARMSG as `channel.chat.message_delete`; CLEARCHAT as `channel.chat.clear_user_messages`, or
    `channel.chat.clear` without a target. A timeout's length, which EventSub's basic tier doesn't give,
    stays under `irc`."""
    base = _broadcaster(line)
    if line.command == "CLEARMSG":
        return base | {
            "target_user_id": _or_none(line.tag("target-user-id")),
            "target_user_login": line.tag("login"),
            "target_user_name": None,
            "message_id": line.tag("target-msg-id"),
            "irc": {"text": line.text},
        }
    target = line.params[1] if len(line.params) > 1 else ""
    if not target:
        return base | {"irc": {}}
    return base | {
        "target_user_id": line.tag("target-user-id"),
        "target_user_login": target,
        "target_user_name": None,
        "irc": _irc_only(line, ("ban-duration",)),
    }
