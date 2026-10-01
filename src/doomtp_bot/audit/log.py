"""Always-on audit log for configuration changes (architecture §5.5), in vex-platform's shared table.

`public.audit_log` is the table twitch-archive writes too (ADR-0027). `write_audit` and `read_audit` keep
the shape they had over `bot.audit_log`, so their callers and the v1 API don't change:

    actor_user_id  -> actor_kind 'user' and actor_id, or actor_kind 'system' when there is none
    channel_id     -> scope (NULL for a global change)
    before, after  -> jsonb: a string that is JSON is stored parsed, any other string as a JSON string
    at (ms)        -> at (timestamptz)

The table is always named with its schema: on the bot's connections an unqualified `audit_log` is still
`bot.audit_log`, which `copy_legacy_audit` copies into the shared table at every start.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from typing import Any

from vex_platform.actor import VIAS
from vex_platform.audit.sql import insert_sql

from doomtp_bot.clock import now_ms
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.runtime.result import to_json
from doomtp_bot.storage.db import Connection, transaction

TABLE = "public.audit_log"
LEGACY = "bot.audit_log"
_INSERT = insert_sql(TABLE, "pyformat")
_VIAS = "(" + ", ".join(f"'{v}'" for v in VIAS) + ")"
# The bot's own names for a surface the shared table names otherwise.
_RENAMED_VIAS = {"script": "cli"}


def _via(via: str) -> tuple[str, dict[str, str] | None]:
    """The shared table's `via`, and a `detail` that keeps a surface it has no name for."""
    via = _RENAMED_VIAS.get(via, via)
    return (via, None) if via in VIAS else ("system", {"via": via})


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON that jsonb accepts")


def _encode(value: Any) -> str | None:
    """JSON text for a jsonb column. A string that is already JSON is kept as it is, since `read_audit`
    used to decode such a string on the way out; any other string becomes a JSON string."""
    if value is None:
        return None
    if not isinstance(value, str):
        return to_json(value)
    try:
        json.loads(value, parse_constant=_no_constant)
    except ValueError:
        return json.dumps(value, ensure_ascii=False)
    return value


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
    `via` "script" is the shared table's "cli"; a surface it has no name for is recorded as "system", with
    the bot's own name in `detail`.
    The row is built here rather than as a vex-platform `AuditEntry`: some of the bot's actions predate the
    dotted `noun.verb` rule (`http_limits`), and an audit row must never be the reason a change fails.
    """
    shared_via, detail = _via(via)
    await conn.execute(
        _INSERT,
        {
            "at": dt.datetime.fromtimestamp(now_ms() / 1000, dt.UTC),
            "actor_kind": "system" if actor_user_id is None else "user",
            "actor_id": actor_user_id,
            "actor_login": None,
            "via": shared_via,
            "action": action,
            "target": target,
            "scope": None if channel_id == GLOBAL else channel_id,
            "outcome": "ok",
            "before": _encode(before),
            "after": _encode(after),
            "detail": None if detail is None else to_json(detail),
            "request_id": None,
            "job_run_id": None,
        },
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

    `actor_user_id` is the Twitch user who made the change, `None` for anyone else (the bot itself, an API
    key). `at` is in ms.
    """
    clauses: list[str] = []
    params: list[object] = []
    if channel_id is not None:
        clauses.append("scope = %s")
        params.append(channel_id)
    elif channel_ids is not None:
        clauses.append("scope = ANY(%s)")
        params.append(list(channel_ids))
    if before_id is not None:
        clauses.append("id < %s")
        params.append(before_id)
    if actor_user_id is not None:
        clauses.append("actor_kind = 'user' AND actor_id = %s")
        params.append(actor_user_id)
    if action is not None:
        clauses.append("action LIKE %s" if action.endswith(".") else "action = %s")
        params.append(
            action.replace("%", r"\%").replace("_", r"\_") + "%" if action.endswith(".") else action
        )
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    async with await conn.execute(
        "SELECT id, scope AS channel_id, CASE WHEN actor_kind = 'user' THEN actor_id END AS actor_user_id,"
        " via, action, target, before, after, round(extract(epoch FROM at) * 1000)::bigint AS at"
        f" FROM {TABLE}{where} ORDER BY id DESC LIMIT %s",
        (*params, limit),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


def _jsonb(column: str) -> str:
    """A text column as jsonb, by the same rule as `_encode`."""
    return f"CASE WHEN pg_input_is_valid({column}, 'jsonb') THEN {column}::jsonb ELSE to_jsonb({column}) END"


async def copy_legacy_audit(conn: Connection) -> int:
    """Copy the rows of `bot.audit_log` not copied yet into the shared table, oldest first, and return how
    many. Each copy's `request_id` is `bot.audit_log:<id>`, which is how a later run knows it is there.

    It runs at every start, so it also picks up what an older image wrote after a rollback; those rows end
    up newer by id than by `at`.
    """
    async with transaction(conn):
        cur = await conn.execute(
            f"INSERT INTO {TABLE} (at, actor_kind, actor_id, via, action, target, scope, before, after,"
            " detail, request_id)"
            " SELECT to_timestamp(b.at / 1000.0),"
            " CASE WHEN b.actor_user_id IS NULL THEN 'system' ELSE 'user' END, b.actor_user_id,"
            # The old column had no check: as `_via` does, a surface the shared table doesn't know is kept
            # in `detail`.
            f" CASE WHEN b.via IN {_VIAS} THEN b.via WHEN b.via = 'script' THEN 'cli' ELSE 'system' END,"
            f" b.action, b.target, b.channel_id, {_jsonb('b.before')}, {_jsonb('b.after')},"
            f" CASE WHEN b.via IN {_VIAS} OR b.via = 'script' THEN NULL ELSE jsonb_build_object('via', b.via) END,"
            f" '{LEGACY}:' || b.id"
            f" FROM {LEGACY} b"
            f" WHERE NOT EXISTS (SELECT FROM {TABLE} p WHERE p.request_id = '{LEGACY}:' || b.id)"
            " ORDER BY b.id"
        )
        return cur.rowcount
