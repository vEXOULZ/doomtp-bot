"""Always-on audit log for configuration changes (architecture §5.5)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Sequence
from typing import Any

from doomtp_bot.clock import now_ms
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.runtime.result import to_json
from doomtp_bot.storage.db import Connection


def _encode(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else to_json(value)


async def write_audit(
    conn: Connection,
    *,
    action: str,
    actor_user_id: str | None,
    via: str,
    channel_id: str | None = None,
    target: str | None = None,
    before: Any = None,
    after: Any = None,
) -> None:
    """Insert one audit row. Callers run this inside the same transaction as the change it records.

    A change to the global scope (`GLOBAL`) is recorded with no channel, whichever way the caller spells it.
    """
    await conn.execute(
        "INSERT INTO audit_log (channel_id, actor_user_id, via, action, target, before, after, at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            None if channel_id == GLOBAL else channel_id,
            actor_user_id,
            via,
            action,
            target,
            _encode(before),
            _encode(after),
            now_ms(),
        ),
    )


async def read_audit(
    conn: Connection,
    *,
    channel_id: str | None = None,
    limit: int = 50,
    channel_ids: Sequence[str] | None = None,
    before_id: int | None = None,
    actor_user_id: str | None = None,
    action: str | None = None,
) -> list[dict[str, Any]]:
    """The newest audit rows first, optionally only one channel's (or only some channels'), with `before`
    and `after` decoded. `before_id` pages back: rows older than that id. `action` matches a whole action
    or, ending in `.`, every action under it (`cc.`).

    Older rows whose values aren't JSON keep the stored string rather than failing the read.
    """
    clauses: list[str] = []
    params: list[object] = []
    if channel_id is not None:
        clauses.append("channel_id = %s")
        params.append(channel_id)
    elif channel_ids is not None:
        clauses.append("channel_id = ANY(%s)")
        params.append(list(channel_ids))
    if before_id is not None:
        clauses.append("id < %s")
        params.append(before_id)
    if actor_user_id is not None:
        clauses.append("actor_user_id = %s")
        params.append(actor_user_id)
    if action is not None:
        clauses.append("action LIKE %s" if action.endswith(".") else "action = %s")
        params.append(
            action.replace("%", r"\%").replace("_", r"\_") + "%" if action.endswith(".") else action
        )
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    async with await conn.execute(
        "SELECT id, channel_id, actor_user_id, via, action, target, before, after, at"
        f" FROM audit_log{where} ORDER BY id DESC LIMIT %s",
        (*params, limit),
    ) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    for entry in rows:
        for field in ("before", "after"):
            if entry[field]:
                with contextlib.suppress(ValueError):  # older rows aren't always JSON
                    entry[field] = json.loads(entry[field])
    return rows
