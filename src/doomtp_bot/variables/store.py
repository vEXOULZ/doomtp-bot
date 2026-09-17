"""SQLite variable store (ADR-0010). Commits are atomic; channel writes and writes to other users' rows are audited."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.runtime.namespaces import CHATTER_KEY
from doomtp_bot.runtime.result import to_json
from doomtp_bot.runtime.values import MISSING
from doomtp_bot.runtime.variables import Space, VarKey, WriteOp, apply_op
from doomtp_bot.storage.db import transaction

if TYPE_CHECKING:
    from doomtp_bot.runtime.context import ExecContext


@dataclass(frozen=True, slots=True)
class Entry:
    key: VarKey
    value: Any
    updated_at: int
    updated_by: str | None


def _chatter_of(key: VarKey) -> str | None:
    column = CHATTER_KEY.get(key.ns)
    return getattr(key, column) if column else None


class SqliteVariableStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def get(self, key: VarKey) -> Any:
        async with self.conn.execute(
            "SELECT value FROM variables WHERE ns = ? AND key1 = ? AND key2 = ? AND key3 = ? AND name = ?",
            (key.ns, key.key1, key.key2, key.key3, key.name),
        ) as cur:
            row = await cur.fetchone()
        return MISSING if row is None else json.loads(row[0])

    async def names_in(self, space: Space) -> set[str]:
        async with self.conn.execute(
            "SELECT name FROM variables WHERE ns = ? AND key1 = ? AND key2 = ? AND key3 = ?",
            (space.ns, space.key1, space.key2, space.key3),
        ) as cur:
            return {r[0] for r in await cur.fetchall()}

    async def entries(self, space: Space) -> list[Entry]:
        async with self.conn.execute(
            "SELECT name, value, updated_at, updated_by FROM variables"
            " WHERE ns = ? AND key1 = ? AND key2 = ? AND key3 = ? ORDER BY name",
            (space.ns, space.key1, space.key2, space.key3),
        ) as cur:
            return [
                Entry(
                    VarKey(space.ns, space.key1, space.key2, space.key3, r[0]), json.loads(r[1]), r[2], r[3]
                )
                for r in await cur.fetchall()
            ]

    async def top(self, ns: str, key1: str, key2: str, name: str, limit: int = 10) -> list[tuple[str, Any]]:
        """Leaderboard over the chatter-keyed column of a space prefix, numeric values only, highest first."""
        column = CHATTER_KEY.get(ns)
        if column is None:
            raise ValueError(f"{ns} has no per-chatter rows")
        where = ["ns = ?", "name = ?", "key1 = ?"]
        params: list[Any] = [ns, name, key1]
        if column == "key3":
            where.append("key2 = ?")
            params.append(key2)
        sql = (
            f"SELECT {column}, value FROM variables WHERE {' AND '.join(where)}"
            " AND json_type(value) IN ('integer', 'real') ORDER BY CAST(value AS REAL) DESC LIMIT ?"
        )
        async with self.conn.execute(sql, (*params, limit)) as cur:
            return [(r[0], json.loads(r[1])) for r in await cur.fetchall()]

    async def commit(self, ops: Iterable[WriteOp], ctx: ExecContext) -> None:
        ops = list(ops)
        if not ops:
            return
        actor = ctx.invoker.id if ctx.invoker else None
        now = now_ms()
        async with transaction(self.conn, immediate=True):
            for op in ops:
                current = await self.get(op.key)
                before = current
                value = apply_op(current, op)
                k = op.key
                if value is MISSING:
                    await self.conn.execute(
                        "DELETE FROM variables WHERE ns = ? AND key1 = ? AND key2 = ? AND key3 = ? AND name = ?",
                        (k.ns, k.key1, k.key2, k.key3, k.name),
                    )
                else:
                    await self.conn.execute(
                        "INSERT INTO variables (ns, key1, key2, key3, name, value, updated_at, updated_by, updated_via)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT (ns, key1, key2, key3, name) DO UPDATE SET value = excluded.value,"
                        " updated_at = excluded.updated_at, updated_by = excluded.updated_by,"
                        " updated_via = excluded.updated_via",
                        (k.ns, k.key1, k.key2, k.key3, k.name, to_json(value),
                         now, actor, ctx.run_id),
                    )  # fmt: skip
                owner = _chatter_of(k)
                if k.ns == "channel" or (owner is not None and owner != actor):
                    await write_audit(
                        self.conn,
                        action=f"variable.{op.kind}",
                        actor_user_id=actor,
                        via="chat" if ctx.trigger_type == "chat" else ctx.trigger_type,
                        channel_id=ctx.channel.id,
                        target=f"{k.ns}.{k.name}" + (f"@{owner}" if owner else ""),
                        before=None if before is MISSING else before,
                        after=None if value is MISSING else value,
                    )
