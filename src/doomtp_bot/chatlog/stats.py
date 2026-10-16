"""Counts over the chat log for the public stats (`/api/v1/stats`): chat lines logged, lines the bot sent and
runs, over the last minute, hour and day. Each count reads a `(channel_id, time)` index, so callers pass the
channels to count and cache the answer for a while.
"""

from __future__ import annotations

from typing import Any

from doomtp_bot.storage.db import Connection, fetch_one

MINUTE_MS, HOUR_MS, DAY_MS = 60_000, 3_600_000, 86_400_000


def _windows(row: dict[str, Any] | None) -> dict[str, int]:
    row = row or {}
    return {"minute": int(row.get("minute") or 0), "hour": int(row.get("hour") or 0), "day": int(row.get("day") or 0)}


async def counts(conn: Connection, channel_ids: list[str], now_ms: int) -> dict[str, Any]:
    """`messages_logged` and `messages_sent` per window, and the day's runs by outcome, in these channels."""
    if not channel_ids:
        empty = _windows(None)
        return {
            "messages_logged": empty,
            "messages_sent": dict(empty),
            "runs_day": {"ok": 0, "failed": 0, "cancelled": 0},
        }
    window = (now_ms - MINUTE_MS, now_ms - HOUR_MS, channel_ids, now_ms - DAY_MS)
    logged = await fetch_one(
        conn,
        "SELECT count(*) FILTER (WHERE sent_at >= %s) AS minute, count(*) FILTER (WHERE sent_at >= %s) AS hour,"
        " count(*) AS day FROM messages WHERE channel_id = ANY(%s) AND sent_at >= %s",
        window,
    )
    # A line the bot sent: in the outbox's log and not dropped (moderated, filtered, rate limited, refused).
    sent = await fetch_one(
        conn,
        "SELECT count(*) FILTER (WHERE at >= %s) AS minute, count(*) FILTER (WHERE at >= %s) AS hour,"
        " count(*) AS day FROM outbound_msgs WHERE channel_id = ANY(%s) AND at >= %s"
        " AND dropped_reason IS NULL",
        window,
    )
    runs = await fetch_one(
        conn,
        "SELECT count(*) FILTER (WHERE cancelled_reason IS NULL AND code = 0) AS ok,"
        " count(*) FILTER (WHERE cancelled_reason IS NULL AND code <> 0) AS failed,"
        " count(*) FILTER (WHERE cancelled_reason IS NOT NULL) AS cancelled"
        " FROM command_runs WHERE channel_id = ANY(%s) AND at >= %s",
        window[2:],
    )
    runs = runs or {}
    return {
        "messages_logged": _windows(logged),
        "messages_sent": _windows(sent),
        "runs_day": {k: int(runs.get(k) or 0) for k in ("ok", "failed", "cancelled")},
    }
