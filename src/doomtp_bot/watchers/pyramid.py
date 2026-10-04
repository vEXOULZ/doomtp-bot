"""Emote pyramids (ADR-0028).

One chatter posts `LUL`, `LUL LUL`, `LUL LUL LUL`, then back down to `LUL`. This watcher reports each
row and how the pyramid ended, and nothing else: chance, sizes that count, exemptions, messages and stats
belong to the `pyramid` pack.

Whether the bot's break landed follows from the order the lines arrive in. If the bot's own line comes
before the builder's last row, the pyramid is `broken` with `by_bot`; if the last row comes first, it is
`complete`, and the bot's line after it is just a line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from doomtp_bot.watchers.base import WatchEvent

if TYPE_CHECKING:
    from doomtp_bot.core.events import ChatMessage

TYPE = "pyramid"
# What chat clients add to get past Twitch's duplicate-message check, plus zero-width spaces and joiners.
_INVISIBLE = re.compile(r"[\U000e0000\u034f\u200b-\u200d\u2060]")


def row_of(text: str) -> tuple[str, int] | None:
    """`LUL LUL LUL` → ("LUL", 3): one token repeated. None for anything else."""
    # A lone U+FE0F is a bypass too; inside a token it belongs to an emoji, so it stays there.
    tokens = [t for t in _INVISIBLE.sub("", text).split() if t != "\ufe0f"]
    if not tokens or any(t != tokens[0] for t in tokens):
        return None
    return tokens[0], len(tokens)


@dataclass(slots=True)
class _Pyramid:
    """A pyramid in progress. At width 1 and before any second row it is only a candidate."""

    channel_id: str
    id: str  # the message id of its first row: unique without a counter that a restart would reset
    builder: tuple[str, str, str]  # (id, login, display)
    badges: frozenset[str]
    token: str
    width: int = 1
    peak: int = 1
    falling: bool = False


def _person(user: tuple[str, str, str]) -> dict[str, str]:
    return {"id": user[0], "name": user[1], "display": user[2]}


class PyramidWatcher:
    types: tuple[str, ...] = (TYPE,)

    def __init__(self) -> None:
        self._open: dict[str, _Pyramid] = {}

    def forget(self, channel_id: str) -> None:
        self._open.pop(channel_id, None)

    def observe(self, msg: ChatMessage) -> list[WatchEvent]:
        row = row_of(msg.text)
        pyramid = self._open.get(msg.channel_id)
        if pyramid is not None:
            same_row = msg.user_id == pyramid.builder[0] and row is not None and row[0] == pyramid.token
            if same_row and row is not None:
                if not pyramid.falling and row[1] == pyramid.width + 1:
                    pyramid.width = pyramid.peak = row[1]
                    return [self._event(pyramid, "step", direction="up")]
                if pyramid.peak >= 2 and row[1] == pyramid.width - 1:
                    pyramid.width, pyramid.falling = row[1], True
                    if pyramid.width == 1:
                        del self._open[msg.channel_id]
                        return [self._event(pyramid, "complete")]
                    return [self._event(pyramid, "step", direction="down")]
            del self._open[msg.channel_id]
            if pyramid.peak >= 2:
                breaker = (msg.user_id, msg.user_login, msg.display_name)
                broken = self._event(
                    pyramid,
                    "broken",
                    breaker=_person(breaker),
                    by_bot=msg.is_self,
                    self_broken=msg.user_id == pyramid.builder[0],
                )
                return [broken, *self._start(msg, row)]
        return self._start(msg, row)

    def _start(self, msg: ChatMessage, row: tuple[str, int] | None) -> list[WatchEvent]:
        """A one-token line may be the top of a new pyramid. The bot never builds one."""
        if row is not None and row[1] == 1 and not msg.is_self:
            self._open[msg.channel_id] = _Pyramid(
                channel_id=msg.channel_id,
                id=msg.message_id,
                builder=(msg.user_id, msg.user_login, msg.display_name),
                badges=frozenset(b.set_id for b in msg.badges),
                token=row[0],
            )
        return []

    @staticmethod
    def _event(pyramid: _Pyramid, phase: str, **extra: Any) -> WatchEvent:
        payload: dict[str, Any] = {
            "pyramid_id": pyramid.id,
            "phase": phase,
            "direction": "",
            "token": pyramid.token,
            "width": pyramid.width,
            "peak": pyramid.peak,
            "user": _person(pyramid.builder),
            "by_bot": False,
            "self_broken": False,
            **extra,
        }
        return WatchEvent(TYPE, pyramid.channel_id, payload, pyramid.builder, pyramid.badges)
