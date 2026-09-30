"""The chat log read as one timeline, a page at a time, and what that stretch of it covers (ADR-0025).

Two readers want the same thing: the web site's log viewer, which opens on the newest lines and pages
back, and the VOD archive, which walks a stream's window oldest first to enrich its chat replay. Both get
**entries** of three kinds, merged in time order:

  * `message` — a `messages` row, with its moderation flags and, for a command or the bot's own reply,
    the run that links them (`command_runs`, via `outbound_msgs` for the reply);
  * `notification` — a `chat_notifications` row (subs, raids, announcements…);
  * `moderation` — a `mod_events` row, with the logins of the target and the moderator.

Pages are keyset pages: the cursor is the last entry's `(at, kind, id)`, so a page is an index range scan
however deep it is, and rows written while someone pages don't shift what they see. Each kind is read on
its own index with the cursor's predicate and `limit + 1` rows, and the merge keeps the first `limit`.

Nothing here hides anything by default. The log keeps what moderators removed (architecture §3.1), and
the caller decides: `hide_removed` leaves out deleted and cleared messages, as chat shows them.
"""

from __future__ import annotations

import base64
import binascii
import heapq
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from doomtp_bot.clock import now_ms
from doomtp_bot.history.backfill import gaps_between
from doomtp_bot.storage.db import Connection

Kind = Literal["message", "notification", "moderation"]
Order = Literal["asc", "desc"]
KINDS: tuple[Kind, ...] = ("message", "notification", "moderation")  # the index is the tie-break rank

# A run is written when it ends and a reply when it is sent, both a little after the message they belong
# to. The window only bounds the lookup to the channel's time index; a run longer than this is not linked.
_RUN_SLACK_MS = 10 * 60_000


class CursorError(ValueError):
    """A cursor this module didn't write, or one written for the other order."""


@dataclass(frozen=True, slots=True)
class Cursor:
    order: Order
    at: int
    rank: int
    key: str | int

    def encode(self) -> str:
        raw = json.dumps([self.order, self.at, self.rank, self.key], separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, text: str) -> Cursor:
        try:
            order, at, rank, key = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        except (binascii.Error, UnicodeDecodeError, ValueError, TypeError) as exc:
            raise CursorError("not a cursor from this API") from exc
        # Moderation ids are numbers and the rest are Twitch's strings; anything else wasn't written here.
        key_type = int if rank == KINDS.index("moderation") else str
        valid = (
            order in ("asc", "desc")
            and type(at) is int
            and type(rank) is int
            and 0 <= rank < len(KINDS)
            and type(key) is key_type
        )
        if not valid:
            raise CursorError("not a cursor from this API")
        return cls(order, at, rank, key)


@dataclass(frozen=True, slots=True)
class _Source:
    kind: Kind
    select: str  # SELECT … FROM <table> <alias> …, so the WHERE clause can name the table by its alias
    alias: str
    at_column: str
    key_column: str
    user_column: str

    @property
    def at(self) -> str:
        return f"{self.alias}.{self.at_column}"

    @property
    def key(self) -> str:
        return f"{self.alias}.{self.key_column}"

    def sort_key(self, row: dict[str, Any]) -> tuple[int, int, str | int]:
        return row[self.at_column], KINDS.index(self.kind), row[self.key_column]


_SOURCES = {
    "message": _Source(
        "message",
        "SELECT m.message_id, m.user_id, m.user_login, m.display_name, m.text, m.message_type, m.badges,"
        " m.fragments, m.bits, m.reply_parent_id, m.reward_id, m.source_channel_id, m.is_self, m.is_command,"
        " m.source, m.sent_at, m.received_at, m.deleted_at, m.cleared_at FROM messages m",
        "m",
        "sent_at",
        "message_id",
        "user_id",
    ),
    "notification": _Source(
        "notification",
        "SELECT n.id, n.user_id, u.login AS user_login, u.display_name, n.type, n.payload, n.source, n.sent_at"
        " FROM chat_notifications n LEFT JOIN users u ON u.user_id = n.user_id",
        "n",
        "sent_at",
        "id",
        "user_id",
    ),
    "moderation": _Source(
        "moderation",
        "SELECT e.id, e.type, e.message_id, e.target_user_id, t.login AS target_login,"
        " e.moderator_user_id, mo.login AS moderator_login, e.duration_s, e.reason, e.source, e.at"
        " FROM mod_events e LEFT JOIN users t ON t.user_id = e.target_user_id"
        " LEFT JOIN users mo ON mo.user_id = e.moderator_user_id",
        "e",
        "at",
        "id",
        "target_user_id",
    ),
}


def _after(source: _Source, cursor: Cursor, rank: int) -> tuple[str, tuple[Any, ...]]:
    """Rows strictly past `cursor` in its order, for the source ranked `rank`, sorting by (at, rank, key)."""
    later, same = (">", ">=") if cursor.order == "asc" else ("<", "<=")
    if rank == cursor.rank:
        # Spelled out rather than as a row comparison, so the planner sees the bound on the indexed column.
        return (
            f" AND {source.at} {same} %s AND ({source.at} {later} %s OR {source.key} {later} %s)",
            (cursor.at, cursor.at, cursor.key),
        )
    # A kind that sorts after the cursor's at the same instant is still ahead of it, and one before isn't.
    ahead = rank > cursor.rank if cursor.order == "asc" else rank < cursor.rank
    return f" AND {source.at} {same if ahead else later} %s", (cursor.at,)


async def _read_source(
    conn: Connection,
    source: _Source,
    channel_id: str,
    *,
    since: int | None,
    until: int | None,
    cursor: Cursor | None,
    order: Order,
    limit: int,
    user_ids: Sequence[str] | None,
    query: str | None,
    hide_removed: bool,
) -> list[dict[str, Any]]:
    where = f" WHERE {source.alias}.channel_id = %s"
    params: tuple[Any, ...] = (channel_id,)
    if since is not None:
        where += f" AND {source.at} >= %s"
        params += (since,)
    if until is not None:
        where += f" AND {source.at} < %s"
        params += (until,)
    if cursor is not None:
        clause, values = _after(source, cursor, KINDS.index(source.kind))
        where += clause
        params += values
    if user_ids is not None:
        where += f" AND {source.alias}.{source.user_column} = ANY(%s)"
        params += (list(user_ids),)
    if query is not None:  # messages only: the caller asks for nothing else when searching
        where += " AND m.tsv @@ websearch_to_tsquery('simple', chatlog_unaccent(%s))"
        params += (query,)
    if hide_removed and source.kind == "message":
        where += " AND m.deleted_at IS NULL AND m.cleared_at IS NULL"
    direction = "ASC" if order == "asc" else "DESC"
    sql = f"{source.select}{where} ORDER BY {source.at} {direction}, {source.key} {direction} LIMIT %s"
    async with await conn.execute(sql, (*params, limit + 1)) as cur:
        return [dict(row) for row in await cur.fetchall()]


def _json(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _user(user_id: str | None, login: str | None, display_name: str | None = None) -> dict[str, Any] | None:
    return None if user_id is None else {"id": user_id, "login": login, "display_name": display_name}


def _message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "message",
        "id": row["message_id"],
        "at": row["sent_at"],
        "user": _user(row["user_id"], row["user_login"], row["display_name"]),
        "text": row["text"],
        "fragments": _json(row["fragments"]),
        "badges": _json(row["badges"]),
        "bits": row["bits"],
        "message_type": row["message_type"],
        "reply_parent_id": row["reply_parent_id"],
        "reward_id": row["reward_id"],
        "source_channel_id": row["source_channel_id"],
        "is_self": row["is_self"],
        "is_command": row["is_command"],
        "source": row["source"],
        "received_at": row["received_at"],
        "deleted_at": row["deleted_at"],
        "cleared_at": row["cleared_at"],
        "run": None,
    }


def _notification(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "notification",
        "id": row["id"],
        "at": row["sent_at"],
        "user": _user(row["user_id"], row["user_login"], row["display_name"]),
        "type": row["type"],
        "payload": _json(row["payload"]),
        "source": row["source"],
    }


def _moderation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "moderation",
        "id": row["id"],
        "at": row["at"],
        "type": row["type"],
        "message_id": row["message_id"],
        "target": _user(row["target_user_id"], row["target_login"]),
        "moderator": _user(row["moderator_user_id"], row["moderator_login"]),
        "duration_s": row["duration_s"],
        "reason": row["reason"],
        "source": row["source"],
    }


_SHAPE = {"message": _message, "notification": _notification, "moderation": _moderation}


async def _link_runs(conn: Connection, channel_id: str, messages: list[dict[str, Any]]) -> None:
    """Fill `run` on command messages (the run they started) and the bot's replies (the run that sent them)."""
    commands = [m["id"] for m in messages if m["is_command"]]
    replies = [m["id"] for m in messages if m["is_self"]]
    if not commands and not replies:
        return
    lo = min(m["at"] for m in messages) - _RUN_SLACK_MS
    hi = max(m["at"] for m in messages) + _RUN_SLACK_MS
    runs: dict[str, dict[str, Any]] = {}
    if commands:
        async with await conn.execute(
            "SELECT trigger_id AS message_id, run_ref, trigger_type, trigger_id, expr, code, message"
            " FROM command_runs WHERE channel_id = %s AND at BETWEEN %s AND %s"
            " AND trigger_type = 'chat' AND trigger_id = ANY(%s) ORDER BY at",
            (channel_id, lo, hi, commands),
        ) as cur:
            for row in await cur.fetchall():
                runs.setdefault(row["message_id"], row)
    if replies:
        async with await conn.execute(
            "SELECT o.twitch_message_id AS message_id, o.run_ref, r.trigger_type, r.trigger_id, r.expr,"
            " r.code, r.message FROM outbound_msgs o"
            " LEFT JOIN command_runs r ON r.run_ref = o.run_ref AND r.channel_id = o.channel_id"
            " WHERE o.channel_id = %s AND o.at BETWEEN %s AND %s AND o.twitch_message_id = ANY(%s)"
            " ORDER BY o.at",
            (channel_id, lo, hi, replies),
        ) as cur:
            for row in await cur.fetchall():
                runs.setdefault(row["message_id"], row)
    for message in messages:
        found = runs.get(message["id"])
        if found is not None:
            message["run"] = {
                "ref": found["run_ref"],
                "trigger_type": found["trigger_type"],
                # For a reply, the message that started the run: the replay can draw the arrow back to it.
                "trigger_id": found["trigger_id"],
                "expr": found["expr"],
                "code": found["code"],
                "message": found["message"],
            }


async def user_ids_for(conn: Connection, login: str) -> list[str]:
    """Every user id that has gone by `login`, so a filter by name survives renames."""
    async with await conn.execute(
        "SELECT DISTINCT user_id FROM user_names WHERE login = %s", (login.lower().lstrip("@"),)
    ) as cur:
        return [row["user_id"] for row in await cur.fetchall()]


async def read(
    conn: Connection,
    channel_id: str,
    *,
    kinds: Sequence[Kind] = KINDS,
    since: int | None = None,
    until: int | None = None,
    cursor: Cursor | None = None,
    order: Order = "desc",
    limit: int = 100,
    user_ids: Sequence[str] | None = None,
    query: str | None = None,
    hide_removed: bool = False,
) -> tuple[list[dict[str, Any]], Cursor | None]:
    """One page of `channel_id`'s timeline in `order`, and the cursor for the next page (None at the end).

    `since` is inclusive and `until` exclusive, both in ms. A `query` searches message text and returns
    messages only. A `cursor` must come from a page read in the same order.
    """
    if cursor is not None and cursor.order != order:
        raise CursorError(f"this cursor pages {cursor.order}, not {order}")
    if query is not None:
        kinds = ("message",)
    pages: list[list[tuple[tuple[int, int, str | int], dict[str, Any]]]] = []
    for kind in dict.fromkeys(kinds):  # once each
        source = _SOURCES[kind]
        rows = await _read_source(
            conn, source, channel_id, since=since, until=until, cursor=cursor, order=order, limit=limit,
            user_ids=user_ids, query=query, hide_removed=hide_removed,
        )  # fmt: skip
        pages.append([(source.sort_key(row), row) for row in rows])
    merged = list(heapq.merge(*pages, key=lambda pair: pair[0], reverse=order == "desc"))
    page = merged[:limit]
    entries = [_SHAPE[KINDS[sort_key[1]]](row) for sort_key, row in page]
    await _link_runs(conn, channel_id, [e for e in entries if e["kind"] == "message"])
    if len(merged) <= limit:
        return entries, None
    at, rank, key = page[-1][0]
    return entries, Cursor(order, at, rank, key)


async def coverage(conn: Connection, channel_id: str, since: int, until: int | None = None) -> dict[str, Any]:
    """When the bot was listening to `channel_id` between `since` and `until` (now by default), and the holes.

    A hole between two sessions is filled if a complete backfill run covered it (ADR-0008); one before the
    log begins or after the bot stopped listening has nothing to fill it. `complete` says whether the
    log has every message of the window that Twitch let it see.
    """
    until = now_ms() if until is None else until
    async with await conn.execute(
        "SELECT started_at, ended_at, end_reason FROM log_sessions WHERE channel_id = %s ORDER BY started_at",
        (channel_id,),
    ) as cur:
        sessions = [dict(row) for row in await cur.fetchall()]
    holes: list[tuple[int, int, str]] = [
        (start, end, "between_sessions")
        for start, end in gaps_between([(s["started_at"], s["ended_at"]) for s in sessions])
    ]
    if not sessions:
        holes.append((since, until, "before_log"))
    else:
        if sessions[0]["started_at"] > since:
            holes.insert(0, (since, sessions[0]["started_at"], "before_log"))
        last_end = sessions[-1]["ended_at"]
        if last_end is not None and last_end < until:
            holes.append((last_end, until, "not_listening"))
    gaps: list[dict[str, Any]] = []
    for start, end, reason in holes:
        if end <= since or start >= until:
            continue
        backfill = None
        if reason == "between_sessions":
            async with await conn.execute(
                "SELECT complete, inserted, error, provider FROM backfill_runs"
                " WHERE channel_id = %s AND gap_from = %s AND gap_to = %s ORDER BY at DESC LIMIT 1",
                (channel_id, start, end),
            ) as cur:
                found = await cur.fetchone()
            backfill = None if found is None else dict(found)
        gaps.append(
            {"from": max(start, since), "to": min(end, until), "reason": reason, "backfill": backfill}
        )
    overlapping = [
        s for s in sessions if s["started_at"] < until and (s["ended_at"] is None or s["ended_at"] > since)
    ]
    return {
        "since": since,
        "until": until,
        "sessions": overlapping,
        "gaps": gaps,
        "complete": all(g["backfill"] is not None and g["backfill"]["complete"] for g in gaps),
    }
