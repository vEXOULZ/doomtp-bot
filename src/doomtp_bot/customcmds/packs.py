"""Packs: named sets of custom commands that publish as a unit (ADR-0012).

A channel accepts *the pack*, not a snapshot of its members, so adding a command to a published pack
makes it available immediately. The pack's name doubles as the module name, so `!module disable
blackjack` turns a whole game off in a channel.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from doomtp_bot.clock import now_ms
from doomtp_bot.customcmds.service import NAME_RE, CustomCommand, CustomCommandError, CustomCommandService
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.storage.db import Connection, Row, fetch_one, transaction

if TYPE_CHECKING:
    pass

# Every built-in module (a test keeps this in step with the registry) plus `custom`, the one custom commands
# share. Importing the registry here instead would be circular.
RESERVED_PACK_NAMES = frozenset(
    {
        "core",
        "core_admin",
        "custom",
        "customcmds",
        "help",
        "basic",
        "logsearch",
        "moderation",
        "quotes",
        "triggers",
        "variables",
    }
)


def new_pack_id() -> str:
    return "pk_" + secrets.token_hex(3)


@dataclass(frozen=True, slots=True)
class Pack:
    id: str
    owner_user_id: str
    name: str
    summary: str
    status: Literal["active", "deleted"]


@dataclass(frozen=True, slots=True)
class PackPublication:
    channel_id: str
    pack_id: str
    published_by: str
    status: Literal["active", "disabled"]

    @property
    def is_global(self) -> bool:
        return self.channel_id == GLOBAL


class PackService:
    """Pack storage. Command lookups go through the CustomCommandService it wraps."""

    def __init__(self, conn: Connection, commands: CustomCommandService) -> None:
        self.conn = conn
        self.commands = commands

    # ── reads ───────────────────────────────────────────────────────────────
    @staticmethod
    def _pack(row: Row) -> Pack:
        return Pack(
            id=row["id"],
            owner_user_id=row["owner_user_id"],
            name=row["name"],
            summary=row["summary"] or "",
            status=row["status"],
        )

    async def by_owner(self, owner_user_id: str, name: str) -> Pack | None:
        row = await fetch_one(
            self.conn,
            "SELECT * FROM custom_command_packs WHERE owner_user_id = %s AND name = %s AND status = 'active'",
            (owner_user_id, name.lower()),
        )
        return self._pack(row) if row else None

    async def by_id(self, pack_id: str) -> Pack | None:
        row = await fetch_one(self.conn, "SELECT * FROM custom_command_packs WHERE id = %s", (pack_id,))
        return self._pack(row) if row else None

    async def owned_by(self, owner_user_id: str) -> list[Pack]:
        async with await self.conn.execute(
            "SELECT * FROM custom_command_packs WHERE owner_user_id = %s AND status = 'active' ORDER BY name",
            (owner_user_id,),
        ) as cur:
            return [self._pack(r) for r in await cur.fetchall()]

    async def members(self, pack_id: str) -> list[CustomCommand]:
        async with await self.conn.execute(
            f"{self.commands._SELECT} JOIN custom_command_pack_members m ON m.command_id = c.id"
            " WHERE m.pack_id = %s AND c.status = 'active' ORDER BY c.name",
            (pack_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [self.commands._command(r) for r in rows]

    async def publications_in(
        self, channel_id: str, *, include_global: bool = False
    ) -> list[tuple[PackPublication, Pack]]:
        scopes = (channel_id, GLOBAL) if include_global else (channel_id,)
        placeholders = ", ".join("%s" for _ in scopes)
        async with await self.conn.execute(
            "SELECT p.*, k.id AS pack_id_, k.owner_user_id, k.name, k.summary, k.status AS pack_status"
            " FROM custom_command_pack_publications p"
            f" JOIN custom_command_packs k ON k.id = p.pack_id WHERE p.channel_id IN ({placeholders})"
            " AND k.status = 'active' ORDER BY k.name",
            scopes,
        ) as cur:
            rows = await cur.fetchall()
        return [
            (
                PackPublication(r["channel_id"], r["pack_id"], r["published_by"], r["status"]),
                Pack(r["pack_id_"], r["owner_user_id"], r["name"], r["summary"] or "", r["pack_status"]),
            )
            for r in rows
        ]

    _WITH_PACK = (
        "SELECT k.id AS pack_id_, k.owner_user_id AS pack_owner, k.name AS pack_name,"
        " k.summary AS pack_summary, k.status AS pack_status, c.*"
    )

    async def find_in_scope(self, channel_id: str, name: str) -> tuple[CustomCommand, Pack] | None:
        """A command named `name` offered by a pack published here, else by one published globally."""
        select = self.commands._SELECT.replace("SELECT c.*", self._WITH_PACK, 1)
        for scope in (channel_id, GLOBAL):
            row = await fetch_one(
                self.conn,
                f"{select}"
                " JOIN custom_command_pack_members m ON m.command_id = c.id"
                " JOIN custom_command_packs k ON k.id = m.pack_id"
                " JOIN custom_command_pack_publications p ON p.pack_id = k.id"
                " WHERE p.channel_id = %s AND p.status = 'active' AND k.status = 'active'"
                " AND c.status = 'active' AND c.name = %s",
                (scope, name.lower()),
            )
            if row is not None:
                pack = Pack(
                    row["pack_id_"],
                    row["pack_owner"],
                    row["pack_name"],
                    row["pack_summary"] or "",
                    row["pack_status"],
                )
                return self.commands._command(row), pack
        return None

    # ── writes ──────────────────────────────────────────────────────────────
    async def create(
        self, *, owner_user_id: str, name: str, summary: str = "", actor_via: str = "chat"
    ) -> Pack:
        name = name.lower()
        if not NAME_RE.match(name):
            raise CustomCommandError("pack names: lowercase letters, digits, _ and -, up to 32")
        if name in RESERVED_PACK_NAMES:
            raise CustomCommandError(f"{name} is a built-in module name")
        if await self.by_owner(owner_user_id, name) is not None:
            raise CustomCommandError(f"you already have a pack named {name}")
        pack_id, ts = new_pack_id(), now_ms()
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO custom_command_packs (id, owner_user_id, name, summary, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (pack_id, owner_user_id, name, summary, ts, ts),
            )
            await self.commands._audit(actor_via, owner_user_id, "pack.create", pack_id, None, {"name": name})
        found = await self.by_id(pack_id)
        assert found is not None
        return found

    async def add_member(self, pack: Pack, command: CustomCommand, *, actor_via: str = "chat") -> None:
        if command.owner_user_id != pack.owner_user_id:
            raise CustomCommandError("a pack holds your own commands")
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO custom_command_pack_members (pack_id, command_id, added_at)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (pack.id, command.id, now_ms()),
            )
            await self.commands._audit(
                actor_via, pack.owner_user_id, "pack.add", pack.id, None, {"command": command.name}
            )

    async def remove_member(self, pack: Pack, command: CustomCommand, *, actor_via: str = "chat") -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "DELETE FROM custom_command_pack_members WHERE pack_id = %s AND command_id = %s",
                (pack.id, command.id),
            )
            if cur.rowcount:
                await self.commands._audit(
                    actor_via, pack.owner_user_id, "pack.rm", pack.id, {"command": command.name}, None
                )
            return bool(cur.rowcount)

    async def delete(self, pack: Pack, *, actor_via: str = "chat") -> None:
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE custom_command_packs SET status = 'deleted', updated_at = %s WHERE id = %s",
                (now_ms(), pack.id),
            )
            await self.commands._audit(actor_via, pack.owner_user_id, "pack.delete", pack.id, pack.name, None)

    async def conflicts(self, channel_id: str, pack: Pack) -> list[str]:
        """Member names already published in this channel by a different command (ADR-0012)."""
        clashes: list[str] = []
        for member in await self.members(pack.id):
            found = await self.commands.publication(channel_id, member.name)
            if found is not None and found[1].id != member.id:
                clashes.append(member.name)
        return clashes

    async def publish(
        self, *, channel_id: str, pack: Pack, published_by: str, actor_via: str = "chat"
    ) -> PackPublication:
        clashes = await self.conflicts(channel_id, pack)
        if clashes:
            raise CustomCommandError(f"already published here by another command: {', '.join(clashes)}")
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO custom_command_pack_publications (channel_id, pack_id, published_by, created_at)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (channel_id, pack_id) DO UPDATE SET status = 'active',"
                " published_by = excluded.published_by",
                (channel_id, pack.id, published_by, now_ms()),
            )
            await self.commands._audit(
                actor_via,
                published_by,
                "pack.publish",
                pack.id,
                None,
                {"channel": channel_id, "name": pack.name},
                channel_id=channel_id,
            )
        return PackPublication(channel_id, pack.id, published_by, "active")

    async def unpublish(
        self, *, channel_id: str, pack: Pack, actor_user_id: str | None, actor_via: str = "chat"
    ) -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "DELETE FROM custom_command_pack_publications WHERE channel_id = %s AND pack_id = %s",
                (channel_id, pack.id),
            )
            if cur.rowcount:
                await self.conn.execute(
                    "DELETE FROM publication_write_grants WHERE channel_id = %s AND command_id IN"
                    " (SELECT command_id FROM custom_command_pack_members WHERE pack_id = %s)",
                    (channel_id, pack.id),
                )
                await self.commands._audit(
                    actor_via,
                    actor_user_id,
                    "pack.unpublish",
                    pack.id,
                    pack.name,
                    None,
                    channel_id=channel_id,
                )
        if cur.rowcount:
            await self.commands._grants_changed()
        return bool(cur.rowcount)


async def custom_modules(
    channel_id: str, packs: PackService | None, commands: CustomCommandService | None
) -> dict[str, Literal["pack", "custom"]]:
    """The module names custom commands bring to a channel, for `!module` and the API's module list.

    Every pack active here or globally, under its own name, and `custom` when any command is published
    here or globally one by one: those run under `custom` (`resolution.spec_for`), and so do personal
    aliases, which a channel can't list but does switch off with it.
    """
    found: dict[str, Literal["pack", "custom"]] = {}
    if packs is not None:
        for publication, pack in await packs.publications_in(channel_id, include_global=True):
            if publication.status == "active":
                found[pack.name] = "pack"
    if commands is not None:
        for scope in (channel_id, GLOBAL):
            if await commands.publications_in(scope):
                found["custom"] = "custom"
                break
    return found
