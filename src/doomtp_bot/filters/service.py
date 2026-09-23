"""Filter storage and the two places filtering happens (architecture §9).

1. **Every outbound message**, in the Outbox, after placeholders are substituted. Mandatory.
2. **Content users store** — custom command names and bodies, variable values — checked at save time.

Entries are held in memory per scope and rebuilt on write, the same shape as the policy snapshot.
"""

from __future__ import annotations

import structlog

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.filters.matcher import Action, ChannelFilter, FilterEntry, FilterError, FilterResult, Kind
from doomtp_bot.filters.matcher import compile_entry as _compile
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.storage.db import Connection, fetch_value, transaction

log = structlog.get_logger(__name__)

KINDS: tuple[Kind, ...] = ("word", "wildcard", "regex", "allow")
ACTIONS: tuple[Action, ...] = ("mask", "replace", "tag", "block")


class FilterService:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._entries: dict[str, list[FilterEntry]] = {}
        self._compiled: dict[str, ChannelFilter] = {}

    async def reload(self) -> None:
        entries: dict[str, list[FilterEntry]] = {}
        async with await self.conn.execute(
            "SELECT id, channel_id, pattern, kind, action, category, replacement, enabled FROM filters"
        ) as cur:
            for row in await cur.fetchall():
                entries.setdefault(row["channel_id"], []).append(
                    FilterEntry(
                        id=row["id"],
                        channel_id=row["channel_id"],
                        pattern=row["pattern"],
                        kind=row["kind"],
                        action=row["action"],
                        category=row["category"] or "",
                        replacement=row["replacement"] or "",
                        enabled=bool(row["enabled"]),
                    )
                )
        self._entries = entries
        self._compiled = {}

    def entries_for(self, channel_id: str) -> list[FilterEntry]:
        """A channel's own entries, then the global ones that apply everywhere."""
        return [*self._entries.get(channel_id, ()), *self._entries.get(GLOBAL, ())]

    def _filter_for(self, channel_id: str) -> ChannelFilter:
        compiled = self._compiled.get(channel_id)
        if compiled is None:
            compiled = self._compiled[channel_id] = ChannelFilter(self.entries_for(channel_id))
        return compiled

    # ── the two entry points ────────────────────────────────────────────────
    def apply(self, channel_id: str, text: str) -> tuple[str | None, list[str]]:
        """Outbox hook: censored text (None blocks the send) and the patterns that matched."""
        result = self._filter_for(channel_id).apply(text)
        if result.changed:
            log.info(
                "filter.applied",
                channel=channel_id,
                blocked=result.blocked,
                patterns=result.patterns(),
            )
        return result.text, result.patterns()

    def check(self, channel_id: str, text: str) -> FilterResult:
        """Save-time check for stored content. The caller decides whether to reject or censor."""
        return self._filter_for(channel_id).apply(text)

    def rejects(self, channel_id: str, text: str) -> list[str]:
        """Patterns that make `text` unacceptable to store. Empty means it's fine."""
        result = self.check(channel_id, text)
        return result.patterns() if result.changed else []

    # ── management ──────────────────────────────────────────────────────────
    async def add(
        self,
        *,
        channel_id: str,
        pattern: str,
        kind: Kind = "word",
        action: Action = "mask",
        category: str = "",
        replacement: str = "",
        actor_user_id: str | None,
    ) -> FilterEntry:
        entry = FilterEntry(0, channel_id, pattern, kind, action, category, replacement)
        _compile(entry)  # raises FilterError before anything is stored
        async with transaction(self.conn):
            entry_id = await fetch_value(
                self.conn,
                "INSERT INTO filters (channel_id, pattern, kind, category, action, replacement,"
                " created_by, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (channel_id, pattern, kind, category or None, action, replacement or None,
                 actor_user_id, now_ms()),
            )  # fmt: skip
            await write_audit(
                self.conn,
                action="filter.add",
                actor_user_id=actor_user_id,
                via="chat",
                channel_id=channel_id,
                target=pattern,
                after={"kind": kind, "action": action},
            )
        await self.reload()
        return FilterEntry(int(entry_id or 0), channel_id, pattern, kind, action, category, replacement)

    async def remove(self, *, channel_id: str, entry_id: int, actor_user_id: str | None) -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "DELETE FROM filters WHERE id = %s AND channel_id = %s", (entry_id, channel_id)
            )
            if cur.rowcount:
                await write_audit(
                    self.conn,
                    action="filter.remove",
                    actor_user_id=actor_user_id,
                    via="chat",
                    channel_id=channel_id,
                    target=str(entry_id),
                )
        if cur.rowcount:
            await self.reload()
        return bool(cur.rowcount)

    async def set_enabled(
        self, *, channel_id: str, entry_id: int, enabled: bool, actor_user_id: str | None
    ) -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "UPDATE filters SET enabled = %s WHERE id = %s AND channel_id = %s",
                (enabled, entry_id, channel_id),
            )
            if cur.rowcount:
                await write_audit(
                    self.conn,
                    action="filter.enable" if enabled else "filter.disable",
                    actor_user_id=actor_user_id,
                    via="chat",
                    channel_id=channel_id,
                    target=str(entry_id),
                )
        if cur.rowcount:
            await self.reload()
        return bool(cur.rowcount)


__all__ = ["ACTIONS", "KINDS", "FilterError", "FilterService"]
