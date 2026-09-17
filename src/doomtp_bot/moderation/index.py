"""ModerationIndex: what mods removed recently, so runs can cancel and replies can be dropped (architecture §8)."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable

from doomtp_bot.clock import now_ms
from doomtp_bot.core.events import ChatCleared, MessageDeleted, UserMessagesCleared

DELETED_TTL_MS = 10 * 60 * 1000
MAX_DELETED = 20_000


class ModerationIndex:
    def __init__(self, clock_ms: Callable[[], int] = now_ms) -> None:
        self._clock_ms = clock_ms
        self._deleted: OrderedDict[str, int] = OrderedDict()  # message_id → recorded at
        self._user_clears: dict[tuple[str, str], int] = {}  # (channel, user) → latest clear time
        self._chat_clears: dict[str, int] = {}  # channel → latest clear time

    def record(self, event: MessageDeleted | UserMessagesCleared | ChatCleared) -> None:
        now = self._clock_ms()
        match event:
            case MessageDeleted(message_id=message_id):
                self._deleted[message_id] = now
                self._deleted.move_to_end(message_id)
                self._prune(now)
            case UserMessagesCleared(channel_id=channel, target_user_id=user, at=at):
                key = (channel, user)
                self._user_clears[key] = max(self._user_clears.get(key, 0), at)
                self._prune_clears(now)
            case ChatCleared(channel_id=channel, at=at):
                self._chat_clears[channel] = max(self._chat_clears.get(channel, 0), at)

    def is_invalidated(
        self, channel_id: str, message_id: str | None, user_id: str | None, sent_at_ms: int
    ) -> bool:
        """True if the triggering message was deleted, or its author/the chat was cleared at or after it was sent."""
        if message_id and message_id in self._deleted:
            return True
        if user_id and self._user_clears.get((channel_id, user_id), -1) >= sent_at_ms:
            return True
        return self._chat_clears.get(channel_id, -1) >= sent_at_ms

    def checker(
        self, channel_id: str, message_id: str | None, user_id: str | None, sent_at_ms: int
    ) -> Callable[[], bool]:
        return lambda: self.is_invalidated(channel_id, message_id, user_id, sent_at_ms)

    def _prune_clears(self, now: int) -> None:
        """Clears only matter to messages still in flight; drop old ones once the map grows."""
        if len(self._user_clears) > MAX_DELETED:
            cutoff = now - DELETED_TTL_MS
            self._user_clears = {k: at for k, at in self._user_clears.items() if at >= cutoff}

    def _prune(self, now: int) -> None:
        while self._deleted and (
            len(self._deleted) > MAX_DELETED or next(iter(self._deleted.values())) < now - DELETED_TTL_MS
        ):
            self._deleted.popitem(last=False)
