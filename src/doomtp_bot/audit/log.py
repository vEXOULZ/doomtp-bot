"""Always-on audit log for configuration changes (architecture §5.5)."""

from __future__ import annotations

from typing import Any

import aiosqlite

from doomtp_bot.clock import now_ms
from doomtp_bot.runtime.result import to_json


def _encode(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else to_json(value)


async def write_audit(
    conn: aiosqlite.Connection,
    *,
    action: str,
    actor_user_id: str | None,
    via: str,
    channel_id: str | None = None,
    target: str | None = None,
    before: Any = None,
    after: Any = None,
) -> None:
    """Insert one audit row. Callers run this inside the same transaction as the change it records."""
    await conn.execute(
        "INSERT INTO audit_log (channel_id, actor_user_id, via, action, target, before, after, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            channel_id,
            actor_user_id,
            via,
            action,
            target,
            _encode(before),
            _encode(after),
            now_ms(),
        ),
    )
