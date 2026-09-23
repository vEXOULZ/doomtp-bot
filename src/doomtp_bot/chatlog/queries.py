"""Reads over the chat log that more than one caller needs (architecture §3.3).

The writer (`writer.py`) owns every write; this module owns the shared reads. Full-text search is here
because the API and the `logsearch` command ask the same question with different audiences: the API
answers an API key or an admin session and sees the whole record, deletions included; chat sees only
what is still visible in chat, so a search never brings back what moderators removed.
"""

from __future__ import annotations

from typing import Any

from doomtp_bot.storage.db import Connection

MAX_QUERY_CHARS = 200

_SEARCH = (
    "SELECT m.message_id, m.user_login, m.display_name, m.text, m.sent_at, m.deleted_at"
    " FROM messages m"
    # websearch_to_tsquery takes whatever a person types: an odd query matches nothing instead of
    # raising (ADR-0014). 'simple', unaccented, as the generated tsv column is.
    " WHERE m.tsv @@ websearch_to_tsquery('simple', chatlog_unaccent(%s))"
    " AND m.channel_id = %s"
)
# In chat: nothing deleted, cleared by a timeout or ban, sent by the bot, or a command to it.
_VISIBLE = " AND m.deleted_at IS NULL AND m.cleared_at IS NULL AND NOT m.is_self AND NOT m.is_command"


async def search_messages(
    conn: Connection,
    channel_id: str,
    query: str,
    *,
    limit: int = 50,
    visible_only: bool = False,
    user_login: str | None = None,
) -> list[dict[str, Any]]:
    """The newest messages in `channel_id` matching `query`, newest first, optionally by one chatter."""
    sql = _SEARCH + (_VISIBLE if visible_only else "") + (" AND m.user_login = %s" if user_login else "")
    params: tuple[Any, ...] = (query[:MAX_QUERY_CHARS], channel_id)
    if user_login:
        params += (user_login.lower(),)
    async with await conn.execute(sql + " ORDER BY m.sent_at DESC LIMIT %s", (*params, limit)) as cur:
        return [dict(row) for row in await cur.fetchall()]
