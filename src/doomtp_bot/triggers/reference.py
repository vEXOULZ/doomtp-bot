"""What the public docs say about triggers: each event's cause and `{event.*}` variables, and the `$` fields.

One source for the site's language page, served by `GET /api/v1/language` (`events`, `bot_field_docs`). A test
checks that every trigger type and every `$` field has an entry, so a new one can't ship undocumented.
`trigger_payload` builds the flat `{event.*}` of the notification types out of Twitch's own detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Twitch's name for a chat notice → the trigger type that listens for it.
NOTICE_TRIGGER_TYPE = {"sub_gift": "gift_sub"}


def _person(raw: Any) -> dict[str, str] | None:
    """`{id, login, display}` as the notification payload keeps a user → `{id, name, display}`."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    name = str(raw.get("login") or raw.get("name") or "")
    return {"id": str(raw["id"]), "name": name, "display": str(raw.get("display") or name)}


def trigger_payload(notice: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The notification's payload plus the flat fields a trigger reads: `user`, and per type `viewers`,
    `tier`, `months`, `recipient`. Twitch's own detail stays under `detail`."""
    if notice not in ("raid", "sub", "resub", "sub_gift", "community_sub_gift"):
        return payload  # follows, redemptions and cheers are built flat already (twitch/mapping.py)
    raw = payload.get("detail")
    detail: dict[str, Any] = raw if isinstance(raw, dict) else {}
    out = dict(payload)
    if notice == "raid":
        out["user"] = _person(detail.get("user")) or _person(payload.get("chatter"))
        out["viewers"] = int(detail.get("viewer_count") or 0)
        return out
    out["user"] = _person(payload.get("chatter"))
    out["tier"] = detail.get("tier")
    if notice == "sub":
        out["months"] = int(detail.get("months") or 1)
    elif notice == "resub":
        out["months"] = int(detail.get("cumulative_months") or detail.get("months") or 0)
        out["streak_months"] = detail.get("streak_months")
        out["prime"] = bool(detail.get("prime"))
        out["gift"] = bool(detail.get("gift"))
    elif notice == "sub_gift":
        out["months"] = int(detail.get("months") or 1)
        out["recipient"] = _person(detail.get("recipient"))
    elif notice == "community_sub_gift":
        out["total"] = int(detail.get("total") or 0)
    return out


@dataclass(frozen=True, slots=True)
class EventDoc:
    fires: str  # what sets it off
    variables: tuple[tuple[str, str], ...] = ()  # ({event.*} name, meaning), beyond `type`
    chatter: str = ""  # who `$chatter` is in the run; empty: nobody
    needs: str = ""  # what the channel must have granted
    match: str = ""  # the trigger's own conditions


_USER = ("user.name, user.display, user.id", "the person behind the event")
_PERSON = "the event's user"

EVENTS: dict[str, EventDoc] = {
    "redemption": EventDoc(
        "a viewer redeems a channel point reward",
        (
            _USER,
            ("input", "the text the viewer entered, if the reward asks for one"),
            ("reward.title, reward.id, reward.cost", "the reward"),
            ("status", "the redemption's status"),
        ),
        _PERSON,
        needs="the broadcaster connects the bot (full tier)",
        match="reward_id",
    ),
    "raid": EventDoc(
        "another channel raids this one",
        (("user.name, user.display, user.id", "the raiding channel"), ("viewers", "how many came with the raid")),
        "the raider",
        match="min_viewers",
    ),
    "sub": EventDoc(
        "a viewer subscribes for the first time",
        (_USER, ("tier", "1000, 2000 or 3000"), ("months", "always 1")),
        "the subscriber",
    ),
    "resub": EventDoc(
        "a viewer renews, and shares it",
        (
            _USER,
            ("tier", "1000, 2000 or 3000"),
            ("months", "total months subscribed"),
            ("streak_months", "months in a row, if shared"),
            ("prime, gift", "true or false"),
            ("text", "the message they added"),
        ),
        "the subscriber",
        match="min_months",
    ),
    "gift_sub": EventDoc(
        "a viewer gifts a sub to one person (a community gift of five fires this five times)",
        (
            ("user.name, user.display, user.id", "the gifter (empty when anonymous)"),
            ("recipient.name, recipient.display, recipient.id", "who got it"),
            ("tier", "1000, 2000 or 3000"),
            ("months", "months gifted"),
        ),
        "the gifter",
    ),
    "cheer": EventDoc(
        "a viewer cheers bits",
        (
            ("user.name, user.display, user.id", "the cheerer (empty when anonymous)"),
            ("bits", "how many"),
            ("text", "the cheer's message"),
            ("anonymous", "true or false"),
        ),
        "the cheerer",
        needs="the broadcaster connects the bot (full tier)",
        match="min_bits",
    ),
    "follow": EventDoc(
        "a viewer follows the channel",
        (_USER, ("followed_at", "when, in epoch milliseconds")),
        "the new follower",
        needs="the bot is a moderator in the channel",
    ),
    "stream_online": EventDoc(
        "the bot's once-a-minute check sees the stream go live",
        (
            ("title, game, viewers", "as Twitch lists the stream"),
            ("started_at", "ISO time the stream started"),
            ("channel.login", "the channel"),
        ),
    ),
    "stream_offline": EventDoc(
        "the same check sees the stream is no longer live",
        (("channel.login", "the channel"),),
    ),
    "pyramid": EventDoc(
        "a chatter builds an emote pyramid, row by row, or breaks one",
        (
            ("phase", "step, complete or broken"),
            ("direction", "up or down, on a step"),
            ("token", "the emote"),
            ("width, peak", "this row's width, and the widest so far"),
            ("user.name, user.display", "the builder"),
            ("breaker.name, breaker.display", "who broke it, on broken"),
            ("by_bot, self_broken", "true or false"),
            ("pyramid_id", "ties the rows of one pyramid together"),
        ),
        "the builder, or the breaker on broken",
    ),
    "timer": EventDoc("its interval passes (every 60s at the least), counted in chat lines and live time as set"),
    "cron": EventDoc("the clock matches its cron schedule"),
    "listener": EventDoc(
        "a chat line matches its regex",
        (("user.name, user.id", "who wrote it"), ("message", "the line"), ("match.*", "the regex's named captures")),
        "the author",
    ),
    "bot_online": EventDoc(
        "the bot is in chat: at start, after an update, or after its Twitch connection was started again. "
        "A channel hears it at most every 15 minutes",
        (
            ("reason", "start, update or reconnect"),
            ("cause", "on reconnect, why the connection was restarted"),
            ("down_s", "seconds it was gone, when known"),
            ("version", "the running version"),
        ),
    ),
    "bot_updated": EventDoc(
        "the version changed since the last run; fires just before bot_online",
        (("version", "the new version"), ("previous", "the version before")),
    ),
    "bot_joined": EventDoc("the bot is asked into the channel (!join or the website), not at every start"),
    "bot_modded": EventDoc("the bot's hourly check sees it became a moderator here"),
    "bot_unmodded": EventDoc("the same check sees it stopped being one"),
}

#: `{$root.field}` descriptions. Every field of `lang.parser.BOT_FIELDS` has one.
BOT_FIELD_DOCS: dict[str, dict[str, str]] = {
    "chatter": {
        "id": "Twitch user id",
        "name": "login, lowercase",
        "display": "display name",
        "rank": "permission rank, a number",
        "roles": "the roles they hold here",
        "is_sub": "subscriber",
        "is_vip": "VIP",
        "is_mod": "moderator",
    },
    "channel": {
        "id": "Twitch channel id",
        "name": "login, lowercase",
        "display": "display name",
        "prefix": "the command sign here",
        "live": "whether the stream is live",
        "title": "stream title, when live",
        "game": "category, when live",
        "viewers": "viewer count, when live",
        "uptime": "seconds since the stream started, when live",
        "next_stream": "the next scheduled stream, from the Twitch schedule",
    },
    "publisher": {"id": "Twitch id", "name": "login", "display": "display name"},
    "bot": {
        "name": "the bot's name",
        "id": "its Twitch id (empty for now)",
        "version": "running version",
        "release": "the release it was built from",
        "commit": "the commit it was built from",
        "uptime": "seconds since this process started",
    },
    "now": {
        "iso": "ISO 8601 time",
        "unix": "epoch seconds",
        "date": "the date",
        "time": "the time",
        "weekday": "day of the week",
    },
}
BOT_ROOT_DOCS: dict[str, str] = {
    "chatter": "who ran the command, or the event's user in a trigger; missing when nobody did",
    "channel": "the channel the line is in",
    "publisher": "who published the custom command being run; only inside one",
    "bot": "the bot itself",
    "now": "the time of the run",
}


def reference() -> dict[str, Any]:
    """The JSON `GET /api/v1/language` serves."""
    return {
        "events": [
            {
                "type": type_,
                "fires": doc.fires,
                "variables": [{"name": n, "meaning": m} for n, m in doc.variables],
                "chatter": doc.chatter,
                "needs": doc.needs,
                "match": doc.match,
            }
            for type_, doc in EVENTS.items()
        ],
        "bot_field_docs": BOT_FIELD_DOCS,
        "bot_root_docs": BOT_ROOT_DOCS,
    }
