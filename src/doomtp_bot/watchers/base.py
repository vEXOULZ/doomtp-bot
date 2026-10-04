"""What a chat watcher is (ADR-0028).

A watcher sees every live chat line in a channel, in the order EventSub delivers them, including the
bot's own lines, other bots and ignored users: everything visible in chat. It keeps a little state per
channel and returns trigger events, which channels react to with `!event` triggers like raids and subs.

`observe` runs inside the event loop on every line, so it is synchronous, in memory, and cheap: no I/O,
no database, nothing slower than a pass over the line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from doomtp_bot.core.events import ChatMessage


@dataclass(frozen=True, slots=True)
class WatchEvent:
    """One trigger event: `type` is a trigger type, `payload` becomes `{event.*}`."""

    type: str
    channel_id: str
    payload: dict[str, Any]
    #: The trigger run's chatter, as (id, login, display), and their badge set ids for `$chatter.rank`.
    user: tuple[str, str, str] | None = None
    badges: frozenset[str] = field(default_factory=frozenset)


class ChatWatcher(Protocol):
    #: The trigger types this watcher emits. The dispatcher only feeds it channels with one of them.
    types: tuple[str, ...]

    def observe(self, msg: ChatMessage) -> list[WatchEvent]: ...

    def forget(self, channel_id: str) -> None:
        """Drop what the watcher holds for a channel that no longer has a trigger of its types."""
