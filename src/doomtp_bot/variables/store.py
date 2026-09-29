"""Postgres variable store (ADR-0010). Commits are atomic; channel writes and writes to other users' rows are audited."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.runtime.namespaces import CHATTER_KEY, VAR_NAMESPACES
from doomtp_bot.runtime.result import json_size, to_json
from doomtp_bot.runtime.values import MISSING
from doomtp_bot.runtime.variables import (
    LIMIT_COLUMNS,
    Limits,
    Space,
    VarKey,
    WriteOp,
    apply_op,
    check_quota,
    check_value_cap,
    owner_of,
)
from doomtp_bot.storage.db import Connection, fetch_all, fetch_one, transaction

if TYPE_CHECKING:
    from doomtp_bot.runtime.context import ExecContext


@dataclass(frozen=True, slots=True)
class Entry:
    key: VarKey
    value: Any
    updated_at: int
    updated_by: str | None


@dataclass(frozen=True, slots=True)
class LimitOverride:
    """One owner's row in `variable_limits`; None means the default applies."""

    quota_bytes: int | None = None
    value_cap_bytes: int | None = None
    list_items: int | None = None
    names_per_space: int | None = None


_COLUMNS = ", ".join(LIMIT_COLUMNS)


def _namespaces_of(kind: str) -> list[str]:
    return [ns for ns in VAR_NAMESPACES if ns.split(".", 1)[0] == kind]


def _chatter_of(key: VarKey) -> str | None:
    column = CHATTER_KEY.get(key.ns)
    return getattr(key, column) if column else None


class PostgresVariableStore:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    async def get(self, key: VarKey) -> Any:
        async with await self.conn.execute(
            "SELECT value FROM variables WHERE ns = %s AND key1 = %s AND key2 = %s AND key3 = %s AND name = %s",
            (key.ns, key.key1, key.key2, key.key3, key.name),
        ) as cur:
            row = await cur.fetchone()
        return MISSING if row is None else json.loads(row["value"])

    async def names_in(self, space: Space) -> set[str]:
        async with await self.conn.execute(
            "SELECT name FROM variables WHERE ns = %s AND key1 = %s AND key2 = %s AND key3 = %s",
            (space.ns, space.key1, space.key2, space.key3),
        ) as cur:
            return {r["name"] for r in await cur.fetchall()}

    async def entries(self, space: Space) -> list[Entry]:
        async with await self.conn.execute(
            "SELECT name, value, updated_at, updated_by FROM variables"
            " WHERE ns = %s AND key1 = %s AND key2 = %s AND key3 = %s ORDER BY name",
            (space.ns, space.key1, space.key2, space.key3),
        ) as cur:
            return [
                Entry(
                    VarKey(space.ns, space.key1, space.key2, space.key3, r["name"]),
                    json.loads(r["value"]),
                    r["updated_at"],
                    r["updated_by"],
                )
                for r in await cur.fetchall()
            ]

    async def entries_of_user(self, user_id: str, limit: int = 1000) -> list[Entry]:
        """Everything one user owns: their `chatter.*`, their `channel.chatter.*` in every channel, and the
        `publisher.*` spaces of their commands. For their own page (ADR-0026)."""
        async with await self.conn.execute(
            "SELECT ns, key1, key2, key3, name, value, updated_at, updated_by FROM variables"
            " WHERE (key1 = %s AND (ns = 'chatter' OR ns LIKE 'publisher%%'))"
            " OR (ns = 'channel.chatter' AND key2 = %s)"
            " ORDER BY ns, key1, key2, key3, name LIMIT %s",
            (user_id, user_id, limit),
        ) as cur:
            return [
                Entry(
                    VarKey(r["ns"], r["key1"], r["key2"], r["key3"], r["name"]),
                    json.loads(r["value"]),
                    r["updated_at"],
                    r["updated_by"],
                )
                for r in await cur.fetchall()
            ]

    async def top(self, ns: str, key1: str, key2: str, name: str, limit: int = 10) -> list[tuple[str, Any]]:
        """Leaderboard over the chatter-keyed column of a space prefix, numeric values only, highest first."""
        column = CHATTER_KEY.get(ns)
        if column is None:
            raise ValueError(f"{ns} has no per-chatter rows")
        where = ["ns = %s", "name = %s", "key1 = %s"]
        params: list[Any] = [ns, name, key1]
        if column == "key3":
            where.append("key2 = %s")
            params.append(key2)
        # Values are JSON text. SQLite told integers from reals with json_type(); jsonb calls both
        # 'number', and casting jsonb straight to numeric sorts them without going through a float.
        sql = (
            f"SELECT {column} AS member, value FROM variables WHERE {' AND '.join(where)}"
            " AND jsonb_typeof(value::jsonb) = 'number'"
            " ORDER BY (value::jsonb)::numeric DESC LIMIT %s"
        )
        rows = await fetch_all(self.conn, sql, (*params, limit))
        return [(r["member"], json.loads(r["value"])) for r in rows]

    # ── storage limits (ADR-0019) ───────────────────────────────────────────
    async def defaults(self) -> Limits:
        row = await fetch_one(self.conn, f"SELECT {_COLUMNS} FROM variable_limits WHERE owner_kind = '*'")
        if row is None:
            return Limits()
        # A column the default row leaves NULL keeps the built-in default.
        return replace(Limits(), **{c: row[c] for c in LIMIT_COLUMNS if row[c] is not None})

    async def override(self, kind: str, owner_id: str) -> LimitOverride | None:
        row = await fetch_one(
            self.conn,
            f"SELECT {_COLUMNS} FROM variable_limits WHERE owner_kind = %s AND owner_id = %s",
            (kind, owner_id),
        )
        return None if row is None else LimitOverride(**{c: row[c] for c in LIMIT_COLUMNS})

    async def overrides(self) -> list[tuple[str, str, LimitOverride]]:
        rows = await fetch_all(
            self.conn,
            f"SELECT owner_kind, owner_id, {_COLUMNS} FROM variable_limits"
            " WHERE owner_kind <> '*' ORDER BY owner_kind, owner_id",
        )
        return [
            (r["owner_kind"], r["owner_id"], LimitOverride(**{c: r[c] for c in LIMIT_COLUMNS})) for r in rows
        ]

    async def limits_for(self, kind: str, owner_id: str) -> Limits:
        """Field by field: the owner's override where it has one, the default otherwise."""
        defaults, own = await self.defaults(), await self.override(kind, owner_id)
        if own is None:
            return defaults
        return replace(
            defaults,
            **{f.name: getattr(own, f.name) for f in fields(own) if getattr(own, f.name) is not None},
        )

    async def usage(self, kind: str, owner_id: str) -> dict[str, int]:
        """Bytes stored per namespace for one owner. Namespaces with nothing stored are left out."""
        rows = await fetch_all(
            self.conn,
            "SELECT ns, coalesce(sum(size_bytes), 0) AS used FROM variables"
            " WHERE ns = ANY(%s) AND key1 = %s GROUP BY ns",
            (_namespaces_of(kind), owner_id),
        )
        return {r["ns"]: int(r["used"]) for r in rows}

    async def set_limit(
        self,
        kind: str,
        owner_id: str,
        field: str,
        value: int | None,
        *,
        actor: str | None,
        via: str,
    ) -> None:
        """Set or clear (`None`) one of an owner's limits; kind '*' changes the default, which can't be
        cleared. Audited as `variable_limits.<field>`."""
        if field not in LIMIT_COLUMNS:
            raise ValueError(field)
        if kind == "*" and value is None:
            raise ValueError("the default can't be cleared")
        async with transaction(self.conn):
            before = await self.override(kind, owner_id)
            await self.conn.execute(
                "INSERT INTO variable_limits (owner_kind, owner_id, " + field + ", updated_at, updated_by)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (owner_kind, owner_id) DO UPDATE SET "
                + field + " = excluded." + field + ", updated_at = excluded.updated_at,"
                " updated_by = excluded.updated_by",
                (kind, owner_id, value, now_ms(), actor),
            )  # fmt: skip
            # An override with nothing left in it is no override at all.
            await self.conn.execute(
                "DELETE FROM variable_limits WHERE owner_kind <> '*' AND owner_kind = %s AND owner_id = %s"
                + "".join(f" AND {c} IS NULL" for c in LIMIT_COLUMNS),
                (kind, owner_id),
            )
            await write_audit(
                self.conn,
                action=f"variable_limits.{field}",
                actor_user_id=actor,
                via=via,
                target=f"{kind}:{owner_id}",
                before=None if before is None else getattr(before, field),
                after=value,
            )

    async def commit(self, ops: Iterable[WriteOp], ctx: ExecContext) -> None:
        ops = list(ops)
        if not ops:
            return
        actor = ctx.invoker.id if ctx.invoker else None
        now = now_ms()
        # One process, one connection, and write_lock serializes us, so read-then-write is safe here.
        # When the web UI becomes a second writer (ADR-0014) this needs SELECT ... FOR UPDATE.
        async with transaction(self.conn):
            limits: dict[tuple[str, str], Limits] = {}
            grown: dict[tuple[str, str], int] = {}
            for op in ops:
                current = await self.get(op.key)
                before = current
                k = op.key
                owner = owner_of(k)
                if owner not in limits:
                    limits[owner] = await self.limits_for(*owner)
                value = apply_op(current, op, limits[owner])
                size = 0 if value is MISSING else json_size(value)
                if value is not MISSING:
                    check_value_cap(k, size, limits[owner])
                grown[owner] = grown.get(owner, 0) + size - (0 if current is MISSING else json_size(current))
                if value is MISSING:
                    await self.conn.execute(
                        "DELETE FROM variables WHERE ns = %s AND key1 = %s AND key2 = %s AND key3 = %s AND name = %s",
                        (k.ns, k.key1, k.key2, k.key3, k.name),
                    )
                else:
                    await self.conn.execute(
                        "INSERT INTO variables (ns, key1, key2, key3, name, value, updated_at, updated_by, updated_via)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (ns, key1, key2, key3, name) DO UPDATE SET value = excluded.value,"
                        " updated_at = excluded.updated_at, updated_by = excluded.updated_by,"
                        " updated_via = excluded.updated_via",
                        (k.ns, k.key1, k.key2, k.key3, k.name, to_json(value),
                         now, actor, ctx.run_id),
                    )  # fmt: skip
                chatter = _chatter_of(k)
                if k.ns == "channel" or (chatter is not None and chatter != actor):
                    await write_audit(
                        self.conn,
                        action=f"variable.{op.kind}",
                        actor_user_id=actor,
                        via="chat" if ctx.trigger_type == "chat" else ctx.trigger_type,
                        channel_id=ctx.channel.id,
                        target=f"{k.ns}.{k.name}" + (f"@{chatter}" if chatter else ""),
                        before=None if before is MISSING else before,
                        after=None if value is MISSING else value,
                    )
            # Checked after the writes, inside the transaction: a failure rolls every write back.
            for owner, delta in grown.items():
                used = sum((await self.usage(*owner)).values())
                check_quota(owner, used, delta > 0, limits[owner])
