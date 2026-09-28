"""Quotes: numbered lines a channel wants to keep (architecture §12).

A quote keeps its number forever. Deleting one hides it and leaves the number taken, so a number said on
stream last year still points at the same line or at nothing, never at a different one. Adding and
deleting are recorded in the audit log, like other changes moderators make to what the bot says.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.policy.repository import Actor
from doomtp_bot.storage.db import Connection, fetch_all, fetch_one, transaction

MAX_QUOTE_CHARS = 400
_COLUMNS = "number, text, game, added_at"


@dataclass(frozen=True, slots=True)
class Quote:
    number: int
    text: str
    game: str | None
    added_at: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Quote:
        return cls(row["number"], row["text"], row["game"], row["added_at"])


class QuoteService:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    async def add(self, channel_id: str, text: str, actor: Actor, *, game: str | None = None) -> Quote:
        """Store a quote under the channel's next number, counting deleted ones as taken."""
        async with transaction(self.conn) as conn:
            row = await fetch_one(
                conn,
                "INSERT INTO quotes (channel_id, number, text, game, added_by, added_at)"
                " SELECT %s, coalesce(max(number), 0) + 1, %s, %s, %s, %s FROM quotes WHERE channel_id = %s"
                f" RETURNING {_COLUMNS}",
                (channel_id, text, game, actor.user_id, now_ms(), channel_id),
            )
            assert row is not None
            quote = Quote.from_row(row)
            await write_audit(
                conn,
                action="quote.add",
                actor_user_id=actor.user_id,
                via=actor.via,
                channel_id=channel_id,
                target=str(quote.number),
                after=text,
            )
        return quote

    async def get(self, channel_id: str, number: int) -> Quote | None:
        row = await fetch_one(
            self.conn,
            f"SELECT {_COLUMNS} FROM quotes WHERE channel_id = %s AND number = %s AND deleted_at IS NULL",
            (channel_id, number),
        )
        return Quote.from_row(row) if row else None

    async def pick(self, channel_id: str, rng: random.Random) -> Quote | None:
        """A random quote, drawn with the run's RNG so a seeded run picks the same one."""
        rows = await fetch_all(
            self.conn,
            "SELECT number FROM quotes WHERE channel_id = %s AND deleted_at IS NULL ORDER BY number",
            (channel_id,),
        )
        return await self.get(channel_id, rng.choice(rows)["number"]) if rows else None

    async def search(self, channel_id: str, words: str) -> list[Quote]:
        """Quotes containing every word, case-insensitively, newest first. A plain scan: a channel's quote
        list stays small enough that an index would cost more than it saves."""
        terms = words.lower().split()
        where = "".join(" AND position(%s in lower(text)) > 0" for _ in terms)
        rows = await fetch_all(
            self.conn,
            f"SELECT {_COLUMNS} FROM quotes WHERE channel_id = %s AND deleted_at IS NULL{where}"
            " ORDER BY number DESC",
            (channel_id, *terms),
        )
        return [Quote.from_row(row) for row in rows]

    async def delete(self, channel_id: str, number: int, actor: Actor) -> Quote | None:
        async with transaction(self.conn) as conn:
            row = await fetch_one(
                conn,
                "UPDATE quotes SET deleted_at = %s, deleted_by = %s"
                " WHERE channel_id = %s AND number = %s AND deleted_at IS NULL"
                f" RETURNING {_COLUMNS}",
                (now_ms(), actor.user_id, channel_id, number),
            )
            if row is None:
                return None
            quote = Quote.from_row(row)
            await write_audit(
                conn,
                action="quote.delete",
                actor_user_id=actor.user_id,
                via=actor.via,
                channel_id=channel_id,
                target=str(number),
                before=quote.text,
            )
        return quote
