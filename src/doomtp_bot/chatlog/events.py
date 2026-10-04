"""Every stored event in EventSub's shape, whatever it was stored as (ADR-0024 §2).

A row's `raw` holds the EventSub `event` object (`eventsub`), an IRC line (`irc`) or an EventSub-shaped
rebuild of an older row (`legacy`). This is the one place that tells them apart: `eventsub` and `legacy`
are read as stored, and an IRC line is converted field by field (`history/irc_convert.py`), with its
IRC-only tags under an `irc` key. Converting on read means a better converter improves every old row.

What IRC lacks and backfill looked up is in the row's `enrichment` (ADR-0024 §3, `history/enrich.py`),
and is applied here: emote details, mentions and cheermotes. Each looked-up value keeps a `source`, so a
reader can tell a mention resolved from the log from one Twitch answered for today.
"""

from __future__ import annotations

import re
from typing import Any

from doomtp_bot.history import irc_convert
from doomtp_bot.history.irc_parse import parse_line

# Whitespace-separated words, and the punctuation a mention may be followed by in chat.
_WORD = re.compile(r"\S+")
_TRAILING = ",.!?:;)"


def _line(raw: dict[str, Any]) -> Any:
    line = parse_line(str(raw.get("line", "")))
    if line is None:
        raise ValueError("an irc row without a line it can parse")
    return line


def message(
    raw_format: str | None, raw: dict[str, Any] | None, enrichment: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A `messages` row's `channel.chat.message` event."""
    if raw_format != "irc" or raw is None:
        return dict(raw or {})
    event = irc_convert.message_event(_line(raw))
    if enrichment:
        event["message"]["fragments"] = apply(event["message"]["fragments"], enrichment)
    return event


def notification(
    raw_format: str | None, raw: dict[str, Any] | None, enrichment: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A `chat_notifications` row's event: `channel.chat.notification` for a chat notice, or the event of
    the subscription it came from (`channel.follow`, `channel.cheer`, a redemption). A `legacy` row keeps
    the payload it had under `legacy`."""
    if raw_format != "irc" or raw is None:
        return dict(raw or {})
    event = irc_convert.notification_event(_line(raw))
    if enrichment:
        event["message"]["fragments"] = apply(event["message"]["fragments"], enrichment)
    return event


def moderation(raw_format: str | None, raw: dict[str, Any] | None) -> dict[str, Any]:
    """A `mod_events` row's event: `channel.chat.message_delete`, `.clear_user_messages` or `.clear`."""
    if raw_format != "irc" or raw is None:
        return dict(raw or {})
    return irc_convert.moderation_event(_line(raw))


# ── enrichment ─────────────────────────────────────────────────────────────
def mentioned(text: str) -> list[str]:
    """The names `@mentioned` in chat text, lower case, without the `@` or trailing punctuation."""
    return [name[1:].lower() for word in _WORD.findall(text) if len(name := word.rstrip(_TRAILING)) > 1
            and name.startswith("@")]  # fmt: skip


def apply(fragments: list[dict[str, Any]], enrichment: dict[str, Any]) -> list[dict[str, Any]]:
    """Twitch-shaped fragments with what enrichment found: emote details, and the mentions and cheermotes
    it recognised, split out of the text fragments as EventSub would have sent them."""
    emotes: dict[str, Any] = enrichment.get("emotes") or {}
    mentions: dict[str, Any] = enrichment.get("mentions") or {}
    cheermotes: dict[str, Any] = enrichment.get("cheermotes") or {}
    found: list[dict[str, Any]] = []
    for fragment in fragments:
        emote = fragment.get("emote")
        if fragment.get("type") == "emote" and emote and emote.get("id") in emotes:
            found.append(fragment | {"emote": {"id": emote["id"], **emotes[emote["id"]]}})
        elif fragment.get("type") == "text" and (mentions or cheermotes):
            found.extend(_split(fragment["text"], mentions, cheermotes))
        else:
            found.append(fragment)
    return found


def _split(text: str, mentions: dict[str, Any], cheermotes: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    at = 0

    def plain(until: int) -> None:
        if until > at:
            parts.append({"type": "text", "text": text[at:until], "cheermote": None, "emote": None, "mention": None})

    for word in _WORD.finditer(text):
        token = word.group()
        if token.lower() in cheermotes:
            plain(word.start())
            parts.append({"type": "cheermote", "text": token, "cheermote": cheermotes[token.lower()],
                          "emote": None, "mention": None})  # fmt: skip
            at = word.end()
            continue
        name = token.rstrip(_TRAILING)
        if name.startswith("@") and name[1:].lower() in mentions:
            plain(word.start())
            end = word.start() + len(name)
            parts.append({"type": "mention", "text": name, "cheermote": None, "emote": None,
                          "mention": mentions[name[1:].lower()]})  # fmt: skip
            at = end
    plain(len(text))
    return parts


# ── our own fragment shape ─────────────────────────────────────────────────
def entry_fragment(fragment: dict[str, Any]) -> dict[str, Any]:
    """A Twitch-shaped fragment in the shape `/log` has always given (`twitch.mapping._fragment`), with
    what Twitch or enrichment says beyond it added: the emote's set, owner and format, the cheermote's
    tier, the GIF's id and URL, and where a looked-up value came from."""
    data: dict[str, Any] = {"type": fragment.get("type"), "text": fragment.get("text")}
    mention = fragment.get("mention")
    if mention:
        data["mention"] = {"id": mention.get("user_id"), "login": mention.get("user_login")}
        data["mention"] |= {k: mention[k] for k in ("user_name", "source") if mention.get(k) is not None}
    emote = fragment.get("emote")
    if emote:
        data["emote_id"] = emote.get("id")
        details = {k: v for k, v in emote.items() if k != "id" and v is not None}
        if details:
            data["emote"] = details
    cheermote = fragment.get("cheermote")
    if cheermote:
        data["cheermote"] = {k: v for k, v in cheermote.items() if v is not None}
    gif = fragment.get("gif")
    if gif:
        data["gif"] = {k: v for k, v in gif.items() if v is not None}
    return data
