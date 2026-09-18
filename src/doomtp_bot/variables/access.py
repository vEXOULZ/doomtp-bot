"""Variable write access (docs/variable-access-matrix.md §3–§4). All variables are readable; only writes are gated."""

from __future__ import annotations

import enum
import re
from typing import TYPE_CHECKING

import aiosqlite

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.lang.parser import Context
from doomtp_bot.storage.db import transaction

if TYPE_CHECKING:
    from doomtp_bot.policy.service import PolicyService
    from doomtp_bot.runtime.context import ExecContext

GRANTABLE_RE = re.compile(r"^channel\.(?:chatter\.)?[a-z][a-z0-9_]{0,31}$")


class Actor(enum.StrEnum):
    TYPED = "typed"
    OWN_CC = "own_cc"
    FOREIGN_LINK = "foreign_link"
    FOREIGN_PUB = "foreign_pub"
    TRIGGER = "trigger"
    CALLBACK = "callback"


def actor_of(ctx: ExecContext) -> Actor:
    if ctx.context is Context.CALLBACK:
        return Actor.CALLBACK
    if ctx.context in (Context.TRIGGER, Context.LISTENER):
        return Actor.TRIGGER
    if ctx.context is Context.BODY and ctx.publisher is not None:
        if ctx.invoker is not None and ctx.publisher.id == ctx.invoker.id:
            return Actor.OWN_CC
        return Actor.FOREIGN_PUB if ctx.publisher.publication else Actor.FOREIGN_LINK
    return Actor.TYPED


class VariableAccessPolicy:
    """Implements runtime.variables.VariableAccess using PolicyService ranks and publication write grants."""

    def __init__(self, policy: PolicyService, conn: aiosqlite.Connection) -> None:
        self.policy = policy
        self.conn = conn
        self._grants: frozenset[tuple[str, str, str]] = frozenset()  # (channel, command id, variable)

    async def reload(self) -> None:
        async with self.conn.execute(
            "SELECT channel_id, command_id, variable FROM publication_write_grants"
        ) as cur:
            self._grants = frozenset((r[0], r[1], r[2]) for r in await cur.fetchall())

    def _has_grant(self, ctx: ExecContext, namespace: str, name: str) -> bool:
        """Grants are per command, not per published name: republishing something else under the same
        name starts with no grants (ADR-0009)."""
        pub = ctx.publisher if ctx.publisher is not None and ctx.publisher.publication else None
        return pub is not None and (ctx.channel.id, pub.command_id, f"{namespace}.{name}") in self._grants

    def can_write(self, ctx: ExecContext, namespace: str, name: str) -> bool:
        actor = actor_of(ctx)
        if actor is Actor.CALLBACK:
            return False
        if namespace.startswith("publisher"):
            return actor in (Actor.OWN_CC, Actor.FOREIGN_LINK, Actor.FOREIGN_PUB)
        if namespace == "chatter":
            return actor in (Actor.TYPED, Actor.OWN_CC)
        if namespace == "channel":
            if actor in (Actor.TYPED, Actor.OWN_CC, Actor.TRIGGER):
                return self.policy.reaches_setting_role(ctx, "channel_var_write_role")
            return actor is Actor.FOREIGN_PUB and self._has_grant(ctx, namespace, name)
        if namespace == "channel.chatter":
            if actor in (Actor.TYPED, Actor.OWN_CC, Actor.TRIGGER):
                return True
            return actor is Actor.FOREIGN_PUB and self._has_grant(ctx, namespace, name)
        return False

    # ── grant management (issued by channel mods on publications) ───────────
    async def set_grant(
        self, channel_id: str, command_id: str, variable: str, granted: bool, actor_user_id: str | None
    ) -> None:
        if not GRANTABLE_RE.match(variable):
            raise ValueError("grants name exact channel.x or channel.chatter.x variables (no wildcards)")
        async with transaction(self.conn):
            if granted:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO publication_write_grants"
                    " (channel_id, command_id, variable, granted_by, granted_at) VALUES (?, ?, ?, ?, ?)",
                    (channel_id, command_id, variable, actor_user_id or "system", now_ms()),
                )
            else:
                await self.conn.execute(
                    "DELETE FROM publication_write_grants WHERE channel_id = ? AND command_id = ? AND variable = ?",
                    (channel_id, command_id, variable),
                )
            await write_audit(
                self.conn,
                action="grant.add" if granted else "grant.revoke",
                actor_user_id=actor_user_id,
                via="chat",
                channel_id=channel_id,
                target=f"{command_id}:{variable}",
            )
        await self.reload()
