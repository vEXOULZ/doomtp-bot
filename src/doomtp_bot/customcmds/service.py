"""Custom command storage and lifecycle (ADR-0009).

Everything a user owns lives in `custom_commands` plus one row per version. Other people reach a command
through a **link** (their own alias) or a channel **publication**; both point at the command id, so edits
are live everywhere at once, and deleting the command breaks them immediately and visibly.

Resolution order in a channel is built-in → publication → the invoker's personal alias (spec §5.1).
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite
import structlog

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.lang import SYNTAX_VERSION
from doomtp_bot.lang.ast import Node
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import Context, ParserParams, parse
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.runtime.result import to_json
from doomtp_bot.storage.db import transaction

log = structlog.get_logger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ID_RE = re.compile(r"^cc_[a-z0-9]{6}$")
MAX_BODY_CHARS = 2000
QUOTA_PER_USER = 50
Status = Literal["active", "deleted", "banned"]


class CustomCommandError(Exception):
    """A rule the user broke: bad name, quota, missing command, not theirs."""


@dataclass(frozen=True, slots=True)
class CustomCommand:
    id: str
    owner_user_id: str
    owner_login: str
    name: str
    body: str
    version: int
    summary: str
    visibility: Literal["private", "shareable"]
    status: Status
    params: tuple[dict[str, Any], ...] = ()

    @property
    def shareable(self) -> bool:
        return self.visibility == "shareable"


@dataclass(frozen=True, slots=True)
class Publication:
    channel_id: str
    name: str
    command_id: str
    published_by: str
    status: Literal["active", "disabled", "orphaned"]
    required_role: str | None
    last_run_version: int | None


def new_id() -> str:
    return "cc_" + secrets.token_hex(3)


class CustomCommandService:
    """Repository plus rules. Parsed bodies are cached per (command id, version)."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        quota: int = QUOTA_PER_USER,
        on_grants_changed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.conn = conn
        self.quota = quota
        # Grants live in the access policy's in-memory snapshot; deleting rows here has to invalidate it.
        self.on_grants_changed = on_grants_changed
        self._asts: dict[tuple[str, int, str], Node] = {}  # (command id, version, channel prefix)

    # ── parsing ─────────────────────────────────────────────────────────────
    @staticmethod
    def parse_body(body: str, prefix: str) -> Node:
        """Parse a body in Body context. Raises ParseError, which callers turn into a usage failure."""
        if len(body) > MAX_BODY_CHARS:
            raise CustomCommandError(f"body too long (max {MAX_BODY_CHARS} characters)")
        return parse(body, Context.BODY, ParserParams(prefix=prefix))

    def ast_for(self, command: CustomCommand, prefix: str) -> Node:
        """The body's AST, parsed once per (version, channel prefix)."""
        key = (command.id, command.version, prefix)
        cached = self._asts.get(key)
        if cached is None:
            cached = self._asts[key] = self.parse_body(command.body, prefix)
        return cached

    # ── reads ───────────────────────────────────────────────────────────────
    async def _row(self, sql: str, params: Sequence[Any]) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    @staticmethod
    def _command(row: aiosqlite.Row) -> CustomCommand:
        return CustomCommand(
            id=row["id"],
            owner_user_id=row["owner_user_id"],
            owner_login=row["owner_login"] or row["owner_user_id"],
            name=row["name"],
            body=row["body"],
            version=row["current_version"],
            summary=row["summary"] or "",
            visibility=row["visibility"],
            status=row["status"],
            params=tuple(json.loads(row["params"] or "[]")),
        )

    _SELECT = (
        "SELECT c.*, v.body FROM custom_commands c"
        " JOIN custom_command_versions v ON v.command_id = c.id AND v.version = c.current_version"
    )
    # Publication columns are aliased, because `name` and `status` exist on both tables.
    _SELECT_PUB = (
        "SELECT c.*, v.body, p.channel_id AS pub_channel, p.name AS pub_name, p.published_by AS pub_by,"
        " p.status AS pub_status, p.required_role AS pub_role, p.last_run_version AS pub_version"
        " FROM custom_commands c"
        " JOIN custom_command_versions v ON v.command_id = c.id AND v.version = c.current_version"
        " JOIN custom_command_publications p ON p.command_id = c.id"
    )

    async def by_id(self, command_id: str, *, include_deleted: bool = False) -> CustomCommand | None:
        row = await self._row(f"{self._SELECT} WHERE c.id = ?", (command_id,))
        if row is None:
            return None
        command = self._command(row)
        return command if include_deleted or command.status == "active" else None

    async def by_owner(self, owner_user_id: str, name: str) -> CustomCommand | None:
        row = await self._row(
            f"{self._SELECT} WHERE c.owner_user_id = ? AND c.name = ? AND c.status = 'active'",
            (owner_user_id, name.lower()),
        )
        return self._command(row) if row else None

    async def owned_by(self, owner_user_id: str) -> list[CustomCommand]:
        async with self.conn.execute(
            f"{self._SELECT} WHERE c.owner_user_id = ? AND c.status = 'active' ORDER BY c.name",
            (owner_user_id,),
        ) as cur:
            return [self._command(r) for r in await cur.fetchall()]

    async def linked_by(self, user_id: str) -> list[tuple[str, CustomCommand]]:
        async with self.conn.execute(
            f"{self._SELECT.replace('SELECT c.*', 'SELECT l.alias AS link_alias, c.*', 1)}"
            " JOIN custom_command_links l ON l.command_id = c.id"
            " WHERE l.user_id = ? AND c.status = 'active' ORDER BY l.alias",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [(r["link_alias"], self._command(r)) for r in rows]

    async def publications_in(self, channel_id: str) -> list[tuple[Publication, CustomCommand]]:
        async with self.conn.execute(
            f"{self._SELECT_PUB} WHERE p.channel_id = ? ORDER BY p.name",
            (channel_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [(self._publication(r), self._command(r)) for r in rows]

    @staticmethod
    def _publication(row: aiosqlite.Row) -> Publication:
        return Publication(
            channel_id=row["pub_channel"],
            name=row["pub_name"],
            command_id=row["id"],
            published_by=row["pub_by"],
            status=row["pub_status"],
            required_role=row["pub_role"],
            last_run_version=row["pub_version"],
        )

    async def publication(self, channel_id: str, name: str) -> tuple[Publication, CustomCommand] | None:
        row = await self._row(
            f"{self._SELECT_PUB} WHERE p.channel_id = ? AND p.name = ? AND p.status = 'active'"
            " AND c.status = 'active'",
            (channel_id, name.lower()),
        )
        return (self._publication(row), self._command(row)) if row else None

    async def publication_in_scope(
        self, channel_id: str, name: str
    ) -> tuple[Publication, CustomCommand] | None:
        """This channel's publication, else one published globally (ADR-0012 derived commands)."""
        return await self.publication(channel_id, name) or await self.publication(GLOBAL, name)

    async def personal(self, user_id: str, alias: str) -> CustomCommand | None:
        row = await self._row(
            f"{self._SELECT} JOIN custom_command_links l ON l.command_id = c.id"
            " WHERE l.user_id = ? AND l.alias = ? AND c.status = 'active'",
            (user_id, alias.lower()),
        )
        return self._command(row) if row else None

    async def versions(self, command_id: str) -> list[tuple[int, str, int]]:
        async with self.conn.execute(
            "SELECT version, body, created_at FROM custom_command_versions"
            " WHERE command_id = ? ORDER BY version DESC",
            (command_id,),
        ) as cur:
            return [(r["version"], r["body"], r["created_at"]) for r in await cur.fetchall()]

    async def count_owned(self, owner_user_id: str) -> int:
        row = await self._row(
            "SELECT COUNT(*) AS n FROM custom_commands WHERE owner_user_id = ? AND status = 'active'",
            (owner_user_id,),
        )
        return int(row["n"]) if row else 0

    async def usage_of(self, command_id: str) -> tuple[int, int]:
        """(links, active publications) — what an edit or delete affects."""
        links = await self._row(
            "SELECT COUNT(*) AS n FROM custom_command_links WHERE command_id = ?", (command_id,)
        )
        pubs = await self._row(
            "SELECT COUNT(*) AS n FROM custom_command_publications WHERE command_id = ? AND status = 'active'",
            (command_id,),
        )
        return (int(links["n"]) if links else 0, int(pubs["n"]) if pubs else 0)

    # ── writes ──────────────────────────────────────────────────────────────
    async def create(
        self, *, owner_user_id: str, owner_login: str, name: str, body: str, actor_via: str = "chat"
    ) -> CustomCommand:
        name = name.lower()
        if not NAME_RE.match(name):
            raise CustomCommandError("command names: lowercase letters, digits, _ and -, up to 32")
        if await self.by_owner(owner_user_id, name) is not None:
            raise CustomCommandError(f"you already have a command named {name}")
        if await self.count_owned(owner_user_id) >= self.quota:
            raise CustomCommandError(f"you've reached the limit of {self.quota} commands")
        command_id, ts = new_id(), now_ms()
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO custom_commands (id, owner_user_id, owner_login, name, current_version,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                (command_id, owner_user_id, owner_login, name, ts, ts),
            )
            await self._add_version(command_id, 1, body)
            await self.conn.execute(
                "INSERT INTO custom_command_links (user_id, alias, command_id, created_at) VALUES (?, ?, ?, ?)",
                (owner_user_id, name, command_id, ts),
            )
            await self._audit(actor_via, owner_user_id, "cc.create", command_id, None, {"name": name})
        found = await self.by_id(command_id)
        assert found is not None
        return found

    async def edit(self, command: CustomCommand, body: str, *, actor_via: str = "chat") -> CustomCommand:
        version = command.version + 1
        async with transaction(self.conn):
            await self._add_version(command.id, version, body)
            await self.conn.execute(
                "UPDATE custom_commands SET current_version = ?, updated_at = ? WHERE id = ?",
                (version, now_ms(), command.id),
            )
            await self._audit(
                actor_via, command.owner_user_id, "cc.edit", command.id, command.body, {"version": version}
            )
        updated = await self.by_id(command.id)
        assert updated is not None
        return updated

    async def revert(self, command: CustomCommand, version: int, *, actor_via: str = "chat") -> CustomCommand:
        row = await self._row(
            "SELECT body FROM custom_command_versions WHERE command_id = ? AND version = ?",
            (command.id, version),
        )
        if row is None:
            raise CustomCommandError(f"{command.name} has no version {version}")
        return await self.edit(command, row["body"], actor_via=actor_via)

    async def delete(self, command: CustomCommand, *, actor_via: str = "chat") -> tuple[int, int]:
        """Soft delete. Links and publications stop working at once; returns what was affected."""
        affected = await self.usage_of(command.id)
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE custom_commands SET status = 'deleted', updated_at = ? WHERE id = ?",
                (now_ms(), command.id),
            )
            await self.conn.execute(
                "UPDATE custom_command_publications SET status = 'orphaned' WHERE command_id = ?",
                (command.id,),
            )
            await self.conn.execute(
                "DELETE FROM publication_write_grants WHERE command_id = ?", (command.id,)
            )
            await self._audit(actor_via, command.owner_user_id, "cc.delete", command.id, command.name, None)
        await self._grants_changed()
        return affected

    async def set_params(
        self, command: CustomCommand, rows: list[dict[str, Any]], *, actor_via: str = "chat"
    ) -> CustomCommand:
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE custom_commands SET params = ?, updated_at = ? WHERE id = ?",
                (to_json(rows), now_ms(), command.id),
            )
            await self._audit(actor_via, command.owner_user_id, "cc.params", command.id, None, rows)
        updated = await self.by_id(command.id)
        assert updated is not None
        return updated

    async def set_summary(self, command: CustomCommand, summary: str, *, actor_via: str = "chat") -> None:
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE custom_commands SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, now_ms(), command.id),
            )
            await self._audit(actor_via, command.owner_user_id, "cc.describe", command.id, None, summary)

    async def set_visibility(
        self, command: CustomCommand, shareable: bool, *, actor_via: str = "chat"
    ) -> None:
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE custom_commands SET visibility = ?, updated_at = ? WHERE id = ?",
                ("shareable" if shareable else "private", now_ms(), command.id),
            )
            await self._audit(
                actor_via, command.owner_user_id, "cc.share", command.id, None, {"shareable": shareable}
            )

    async def link(
        self, *, user_id: str, alias: str, command: CustomCommand, actor_via: str = "chat"
    ) -> None:
        alias = alias.lower()
        if not NAME_RE.match(alias):
            raise CustomCommandError("aliases: lowercase letters, digits, _ and -, up to 32")
        if await self.personal(user_id, alias) is not None:
            raise CustomCommandError(f"you already have an alias named {alias}")
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO custom_command_links (user_id, alias, command_id, created_at) VALUES (?, ?, ?, ?)",
                (user_id, alias, command.id, now_ms()),
            )
            await self._audit(actor_via, user_id, "cc.link", command.id, None, {"alias": alias})

    async def unlink(self, *, user_id: str, alias: str, actor_via: str = "chat") -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "DELETE FROM custom_command_links WHERE user_id = ? AND alias = ?", (user_id, alias.lower())
            )
            if cur.rowcount:
                await self._audit(actor_via, user_id, "cc.unlink", alias, None, None)
            return bool(cur.rowcount)

    async def publish(
        self,
        *,
        channel_id: str,
        name: str,
        command: CustomCommand,
        published_by: str,
        required_role: str | None = None,
        actor_via: str = "chat",
    ) -> Publication:
        name = name.lower()
        if not NAME_RE.match(name):
            raise CustomCommandError("published names: lowercase letters, digits, _ and -, up to 32")
        existing = await self._row(
            "SELECT command_id FROM custom_command_publications WHERE channel_id = ? AND name = ?",
            (channel_id, name),
        )
        if existing is not None and existing["command_id"] != command.id:
            raise CustomCommandError(f"{name} is already published here by another command")
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO custom_command_publications (channel_id, name, command_id, published_by,"
                " status, required_role, created_at) VALUES (?, ?, ?, ?, 'active', ?, ?)"
                " ON CONFLICT (channel_id, name) DO UPDATE SET command_id = excluded.command_id,"
                " published_by = excluded.published_by, status = 'active',"
                " required_role = excluded.required_role",
                (channel_id, name, command.id, published_by, required_role, now_ms()),
            )
            await self._audit(
                actor_via, published_by, "cc.publish", command.id, None, {"channel": channel_id, "name": name},
                channel_id=channel_id,
            )  # fmt: skip
        found = await self.publication(channel_id, name)
        assert found is not None
        return found[0]

    async def set_publication_status(
        self,
        *,
        channel_id: str,
        name: str,
        status: Literal["active", "disabled"],
        actor_user_id: str | None,
        actor_via: str = "chat",
    ) -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "UPDATE custom_command_publications SET status = ? WHERE channel_id = ? AND name = ?"
                " AND status != 'orphaned'",
                (status, channel_id, name.lower()),
            )
            if cur.rowcount:
                await self._audit(
                    actor_via, actor_user_id, f"cc.{status}", name, None, None, channel_id=channel_id
                )
            return bool(cur.rowcount)

    async def unpublish(
        self, *, channel_id: str, name: str, actor_user_id: str | None, actor_via: str = "chat"
    ) -> str | None:
        """Remove a publication and its write grants. Returns the command id it pointed at."""
        row = await self._row(
            "SELECT command_id FROM custom_command_publications WHERE channel_id = ? AND name = ?",
            (channel_id, name.lower()),
        )
        if row is None:
            return None
        async with transaction(self.conn):
            await self.conn.execute(
                "DELETE FROM custom_command_publications WHERE channel_id = ? AND name = ?",
                (channel_id, name.lower()),
            )
            await self.conn.execute(
                "DELETE FROM publication_write_grants WHERE channel_id = ? AND command_id = ?",
                (channel_id, row["command_id"]),
            )
            await self._audit(
                actor_via, actor_user_id, "cc.unpublish", row["command_id"], name, None, channel_id=channel_id
            )
        await self._grants_changed()
        return str(row["command_id"])

    async def touch_run(self, publication: Publication, version: int) -> None:
        """Record the version a channel last ran, for the 'changed since' notice (ADR-0009)."""
        if publication.last_run_version == version:
            return
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE custom_command_publications SET last_run_version = ? WHERE channel_id = ? AND name = ?",
                (version, publication.channel_id, publication.name),
            )

    # ── internals ───────────────────────────────────────────────────────────
    async def _grants_changed(self) -> None:
        if self.on_grants_changed is not None:
            await self.on_grants_changed()

    async def _add_version(self, command_id: str, version: int, body: str) -> None:
        await self.conn.execute(
            "INSERT INTO custom_command_versions (command_id, version, body, syntax_version, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (command_id, version, body, SYNTAX_VERSION, now_ms()),
        )
        for cached in [k for k in self._asts if k[0] == command_id]:
            self._asts.pop(cached, None)

    async def _audit(
        self,
        via: str,
        actor_user_id: str | None,
        action: str,
        target: str,
        before: object,
        after: object,
        channel_id: str | None = None,
    ) -> None:
        await write_audit(
            self.conn,
            action=action,
            actor_user_id=actor_user_id,
            via=via,
            channel_id=channel_id,
            target=target,
            before=before,
            after=after,
        )


def parse_errors_to_usage(exc: ParseError | CustomCommandError) -> str:
    return str(exc)


def command_ids(commands: Iterable[CustomCommand]) -> set[str]:
    return {c.id for c in commands}
