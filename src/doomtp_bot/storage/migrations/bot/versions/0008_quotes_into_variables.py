"""Quotes move out of their table into the bot's `publisher.channel` variables (ADR-0019 item 9).

The `quotes` pack the bot publishes keeps them there: `publisher.channel.quotes` is a map from a quote's
number to `{text, date, game?}`, and `publisher.channel.quote_next` the last number given out, deleted
quotes included, so a number is never given twice. Deleted quotes aren't copied.

The variables belong to the bot's account, so upgrading a database with quotes in it needs that account
signed in (`oauth_tokens`). A channel's quotes are one value, and the bot's variables share one quota, so
the bot's publisher limits are raised when the copy wouldn't fit in them; never lowered.

Revision ID: 0008
Revises: 0007
"""

import json
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

NS = "publisher.channel"
QUOTES, NEXT = "quotes", "quote_next"
_TABLE = """
    CREATE TABLE quotes (
        channel_id  text NOT NULL,
        number      integer NOT NULL CHECK (number > 0),
        text        text NOT NULL,
        game        text,
        added_by    text,
        added_at    bigint NOT NULL,
        deleted_by  text,
        deleted_at  bigint,
        PRIMARY KEY (channel_id, number)
    )
"""


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)  # runtime.result.to_json


def _day(added_at: int, timezone: str | None) -> str:
    """The day it was added where the channel is, as `$now.date` would have said then."""
    moment = datetime.fromtimestamp(added_at / 1000, UTC)
    with suppress(ZoneInfoNotFoundError, ValueError):
        moment = moment.astimezone(ZoneInfo(timezone or "UTC"))
    return moment.date().isoformat()


def _bot_id(conn: sa.Connection) -> str | None:
    return conn.execute(sa.text("SELECT user_id FROM oauth_tokens WHERE identity = 'bot'")).scalar()


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT q.channel_id, q.number, q.text, q.game, q.added_at, q.deleted_at, c.timezone"
            " FROM quotes q LEFT JOIN channels c USING (channel_id) ORDER BY q.channel_id, q.number"
        )
    ).mappings()
    channels: dict[str, tuple[dict[str, Any], int]] = {}
    for row in rows:
        kept, last = channels.get(row["channel_id"], ({}, 0))
        if row["deleted_at"] is None:
            quote = {"text": row["text"], "date": _day(row["added_at"], row["timezone"])}
            if row["game"]:
                quote["game"] = row["game"]
            kept[str(row["number"])] = quote
        channels[row["channel_id"]] = (kept, max(last, row["number"]))

    if channels:
        bot = _bot_id(conn)
        if bot is None:
            raise RuntimeError(
                "there are quotes to move into the bot's variables, but no bot account is signed in:"
                " sign the bot in (/auth) with the previous version, then upgrade again"
            )
        now, largest = int(time.time() * 1000), 0
        insert = sa.text(
            "INSERT INTO variables (ns, key1, key2, name, value, updated_at, updated_via)"
            " VALUES (:ns, :bot, :channel, :name, :value, :now, 'migration')"
        )
        for channel_id, (kept, last) in channels.items():
            values = [(NEXT, _json(last))] + ([(QUOTES, _json(kept))] if kept else [])
            for name, value in values:
                conn.execute(
                    insert,
                    {"ns": NS, "bot": bot, "channel": channel_id, "name": name, "value": value, "now": now},
                )
                largest = max(largest, len(value.encode()))
        _make_room(conn, bot, largest, now)

    op.execute("DROP TABLE quotes")


def _make_room(conn: sa.Connection, bot: str, largest: int, now: int) -> None:
    """Raise the bot's publisher quota and value cap to twice what the copy needs, if they are lower."""
    used = (
        conn.execute(
            sa.text(
                "SELECT coalesce(sum(size_bytes), 0) FROM variables WHERE ns LIKE 'publisher%' AND key1 = :bot"
            ),
            {"bot": bot},
        ).scalar()
        or 0
    )
    limits = (
        conn.execute(
            sa.text(
                "SELECT coalesce(o.quota_bytes, d.quota_bytes) AS quota,"
                " coalesce(o.value_cap_bytes, d.value_cap_bytes) AS cap"
                " FROM variable_limits d LEFT JOIN variable_limits o"
                " ON o.owner_kind = 'publisher' AND o.owner_id = :bot WHERE d.owner_kind = '*'"
            ),
            {"bot": bot},
        )
        .mappings()
        .one()
    )
    quota = 2 * int(used) if int(used) > limits["quota"] else None
    cap = 2 * largest if largest > limits["cap"] else None
    if quota is None and cap is None:
        return
    conn.execute(
        sa.text(
            "INSERT INTO variable_limits (owner_kind, owner_id, quota_bytes, value_cap_bytes, updated_at,"
            " updated_by) VALUES ('publisher', :bot, :quota, :cap, :now, NULL)"
            " ON CONFLICT (owner_kind, owner_id) DO UPDATE SET"
            " quota_bytes = coalesce(excluded.quota_bytes, variable_limits.quota_bytes),"
            " value_cap_bytes = coalesce(excluded.value_cap_bytes, variable_limits.value_cap_bytes),"
            " updated_at = excluded.updated_at"
        ),
        {"bot": bot, "quota": quota, "cap": cap, "now": now},
    )


def downgrade() -> None:
    """Back into the table. Each channel's last number comes back as a deleted row when that quote is
    gone, so the old code keeps counting from it; added_at is the start of the day the quote was added."""
    op.execute(_TABLE)
    conn = op.get_bind()
    bot = _bot_id(conn)
    if bot is None:
        return
    rows = conn.execute(
        sa.text(
            "SELECT key2, name, value FROM variables WHERE ns = :ns AND key1 = :bot AND name IN (:q, :n)"
        ),
        {"ns": NS, "bot": bot, "q": QUOTES, "n": NEXT},
    ).mappings()
    channels: dict[str, dict[str, Any]] = {}
    for row in rows:
        channels.setdefault(row["key2"], {})[row["name"]] = json.loads(row["value"])
    insert = sa.text(
        "INSERT INTO quotes (channel_id, number, text, game, added_at, deleted_at)"
        " VALUES (:channel, :number, :text, :game, :added_at, :deleted_at)"
    )
    for channel_id, held in channels.items():
        stored = held.get(QUOTES)
        kept: dict[str, Any] = stored if isinstance(stored, dict) else {}
        numbers = set()
        for key, quote in kept.items():
            if not (key.isdigit() and int(key) > 0 and isinstance(quote, dict) and "text" in quote):
                continue
            added = _start_of(quote.get("date"))
            conn.execute(
                insert,
                {"channel": channel_id, "number": int(key), "text": _text(quote["text"]),
                 "game": quote.get("game"), "added_at": added, "deleted_at": None},
            )  # fmt: skip
            numbers.add(int(key))
        last = held.get(NEXT)
        if isinstance(last, int) and last > 0 and last not in numbers:
            conn.execute(
                insert,
                {"channel": channel_id, "number": last, "text": "", "game": None, "added_at": 0,
                 "deleted_at": 0},
            )  # fmt: skip
    conn.execute(
        sa.text("DELETE FROM variables WHERE ns = :ns AND key1 = :bot AND name IN (:q, :n)"),
        {"ns": NS, "bot": bot, "q": QUOTES, "n": NEXT},
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else _json(value)


def _start_of(date: Any) -> int:
    with suppress(TypeError, ValueError):
        return int(datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp() * 1000)
    return 0
