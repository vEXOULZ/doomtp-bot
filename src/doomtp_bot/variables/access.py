"""Variable write access (docs/variable-access-matrix.md §3–§4). All variables are readable; only writes are gated."""

from __future__ import annotations

import enum
import re
from typing import TYPE_CHECKING

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.lang.parser import Context
from doomtp_bot.storage.db import Connection, transaction

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

    def __init__(self, policy: PolicyService, conn: Connection) -> None:
        self.policy = policy
        self.conn = conn
        self._grants: frozenset[tuple[str, str, str]] = frozenset()  # (channel, command id, variable)

    async def reload(self) -> None:
        async with await self.conn.execute(
            "SELECT channel_id, command_id, variable FROM publication_write_grants"
        ) as cur:
            self._grants = frozenset(
                (r["channel_id"], r["command_id"], r["variable"]) for r in await cur.fetchall()
            )

    def _has_grant(self, ctx: ExecContext, namespace: str, name: str) -> bool:
        """Grants are per command, not per published name: republishing something else under the same
        name starts with no grants (ADR-0009)."""
        pub = ctx.publisher if ctx.publisher is not None and ctx.publisher.publication else None
        return pub is not None and (ctx.channel.id, pub.command_id, f"{namespace}.{name}") in self._grants

    def granted(self, channel_id: str, command_id: str) -> frozenset[str]:
        """Which variables a published command may write in this channel."""
        return frozenset(v for c, cmd, v in self._grants if c == channel_id and cmd == command_id)

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
                    "INSERT INTO publication_write_grants"
                    " (channel_id, command_id, variable, granted_by, granted_at) VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (channel_id, command_id, variable) DO UPDATE SET"
                    " granted_by = EXCLUDED.granted_by, granted_at = EXCLUDED.granted_at",
                    (channel_id, command_id, variable, actor_user_id or "system", now_ms()),
                )
            else:
                await self.conn.execute(
                    "DELETE FROM publication_write_grants WHERE channel_id = %s AND command_id = %s AND variable = %s",
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
