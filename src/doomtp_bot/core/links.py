"""Links in bot output (ADR-0019): clickable only where the bot itself may post them.

Twitch removes or holds links from chatters who aren't moderators or VIPs, and a bot that posts links a
chatter fed it through `echo` would be an easy way around a channel's link rules. So every line the bot
sends goes through one rule: where the bot is a moderator or VIP, or in its own channel, links go out
as they are. Everywhere else each `.` in a link's host becomes ` dot `, which Twitch doesn't link.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from doomtp_bot.core.events import Badge

# A host Twitch would turn into a link: labels joined by dots, ending in an alphabetic TLD, with or
# without a scheme. Version numbers (`v1.2`) and abbreviations (`e.g.`) don't match.
_HOST = re.compile(
    r"(?i)(?<![\w.-])(?P<scheme>[a-z][a-z0-9+.-]*://)?"
    r"(?P<host>(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])"
)

# Badge sets that let an account post links (Twitch's own rule for chat).
LINK_BADGES = frozenset({"broadcaster", "moderator", "vip"})


def defang_links(text: str) -> str:
    """`see example.com/x` -> `see example dot com/x`: the text survives, the link doesn't."""
    return _HOST.sub(lambda m: (m["scheme"] or "") + m["host"].replace(".", " dot "), text)


class BotBadges:
    """The badges on the bot's latest message in each channel, as Twitch reported them.

    Twitch has no cheap way to ask whether the bot is a VIP somewhere, but the bot's own messages come
    back through EventSub with its badges on them, and the chat log keeps them (`messages.is_self`).
    Until the bot has spoken in a channel it counts as neither moderator nor VIP there.
    """

    def __init__(self) -> None:
        self._sets: dict[str, frozenset[str]] = {}

    def saw(self, channel_id: str, badges: Iterable[Badge]) -> None:
        self._sets[channel_id] = frozenset(b.set_id for b in badges)

    def load(self, rows: Iterable[tuple[str, str | None]]) -> None:
        """Seed from the chat log: (channel_id, badges JSON) of the bot's latest message per channel."""
        for channel_id, raw in rows:
            try:
                parsed = json.loads(raw) if raw else []
            except ValueError:
                continue
            self._sets[channel_id] = frozenset(
                b["set_id"] for b in parsed if isinstance(b, dict) and isinstance(b.get("set_id"), str)
            )

    def may_link(self, channel_id: str) -> bool:
        return bool(self._sets.get(channel_id, frozenset()) & LINK_BADGES)
