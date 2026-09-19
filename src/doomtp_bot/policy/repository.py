"""Writes to policy tables. Every change is one transaction together with its audit row (ADR-0006 §5)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.lang.parser import DEFAULT_PREFIX
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.storage.db import transaction


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: str | None
    via: str = "chat"  # chat | api | web | system


class PolicyRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def _audit(
        self, actor: Actor, action: str, channel_id: str | None, target: str, before: object, after: object
    ) -> None:
        await write_audit(
            self.conn,
            action=action,
            actor_user_id=actor.user_id,
            via=actor.via,
            channel_id=None if channel_id == GLOBAL else channel_id,
            target=target,
            before=before,
            after=after,
        )

    async def _one(self, sql: str, params: tuple[object, ...]) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    # ── channels ────────────────────────────────────────────────────────────
    async def ensure_channel(
        self, channel_id: str, login: str, actor: Actor, prefix: str = DEFAULT_PREFIX
    ) -> bool:
        """Create the channel row if missing. Returns True if it was created."""
        async with transaction(self.conn):
            existing = await self._one("SELECT login FROM channels WHERE channel_id = ?", (channel_id,))
            ts = now_ms()
            if existing is not None:
                if existing["login"] != login:
                    await self.conn.execute(
                        "UPDATE channels SET login = ?, updated_at = ? WHERE channel_id = ?",
                        (login, ts, channel_id),
                    )
                return False
            await self.conn.execute(
                "INSERT INTO channels (channel_id, login, prefix, joined_by, added_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (channel_id, login, prefix, actor.user_id, ts, ts),
            )
            await self._audit(actor, "channel.join", channel_id, login, None, {"login": login})
            return True

    async def set_channel_field(self, channel_id: str, column: str, value: object, actor: Actor) -> None:
        allowed = {
            "prefix", "active", "status", "tier", "capabilities", "reply_hold_ms", "log_enabled", "history_backfill",
            "quiet_errors", "timezone", "channel_var_write_role", "grant_min_role", "publish_min_role",
            "create_min_role", "var_admin_role", "automod_action", "automod_timeout_s",
        }  # fmt: skip
        if column not in allowed:
            raise ValueError(f"unknown channel setting {column}")
        async with transaction(self.conn):
            before = await self._one(
                f"SELECT {column} AS v FROM channels WHERE channel_id = ?", (channel_id,)
            )
            stored = (
                json.dumps(sorted(value))
                if column == "capabilities" and isinstance(value, (set, frozenset, list))
                else value
            )
            await self.conn.execute(
                f"UPDATE channels SET {column} = ?, updated_at = ? WHERE channel_id = ?",
                (stored, now_ms(), channel_id),
            )
            await self._audit(
                actor, f"channel.set.{column}", channel_id, column, before["v"] if before else None, stored
            )

    # ── roles ───────────────────────────────────────────────────────────────
    async def create_role(self, channel_id: str, name: str, rank: int, actor: Actor) -> int:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "INSERT INTO roles (channel_id, name, rank, builtin, created_by, created_at) VALUES (?, ?, ?, 0, ?, ?)",
                (channel_id, name, rank, actor.user_id, now_ms()),
            )
            await self._audit(actor, "role.create", channel_id, name, None, {"rank": rank})
            return int(cur.lastrowid or 0)

    async def delete_role(self, role_id: int, actor: Actor) -> None:
        async with transaction(self.conn):
            row = await self._one(
                "SELECT channel_id, name, rank FROM roles WHERE id = ? AND builtin = 0", (role_id,)
            )
            if row is None:
                return
            await self.conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))
            await self._audit(
                actor, "role.delete", row["channel_id"], row["name"], {"rank": row["rank"]}, None
            )

    async def add_member(
        self,
        role_id: int,
        channel_id: str,
        role_name: str,
        user_id: str,
        user_login: str,
        expires_at: int | None,
        actor: Actor,
    ) -> None:
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO role_members (role_id, user_id, user_login, granted_by, granted_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (role_id, user_id) DO UPDATE SET user_login = excluded.user_login,"
                " granted_by = excluded.granted_by, granted_at = excluded.granted_at, expires_at = excluded.expires_at",
                (role_id, user_id, user_login, actor.user_id, now_ms(), expires_at),
            )
            await self._audit(
                actor,
                "role.grant",
                channel_id,
                f"{role_name}:{user_id}",
                None,
                {"login": user_login, "expires_at": expires_at},
            )

    async def remove_member(
        self, role_id: int, channel_id: str, role_name: str, user_id: str, actor: Actor
    ) -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "DELETE FROM role_members WHERE role_id = ? AND user_id = ?", (role_id, user_id)
            )
            if cur.rowcount:
                await self._audit(
                    actor, "role.revoke", channel_id, f"{role_name}:{user_id}", {"member": True}, None
                )
            return bool(cur.rowcount)

    async def members(self, role_id: int) -> list[tuple[str, str | None, int | None]]:
        async with self.conn.execute(
            "SELECT user_id, user_login, expires_at FROM role_members WHERE role_id = ? ORDER BY user_login",
            (role_id,),
        ) as cur:
            return [(r["user_id"], r["user_login"], r["expires_at"]) for r in await cur.fetchall()]

    async def set_global_admin(self, user_id: str, user_login: str, enabled: bool, actor: Actor) -> None:
        async with transaction(self.conn):
            if enabled:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO global_admins (user_id, user_login, granted_by, granted_at) VALUES (?, ?, ?, ?)",
                    (user_id, user_login, actor.user_id or "system", now_ms()),
                )
            else:
                await self.conn.execute("DELETE FROM global_admins WHERE user_id = ?", (user_id,))
            await self._audit(
                actor,
                "admin.grant" if enabled else "admin.revoke",
                GLOBAL,
                user_id,
                None,
                {"login": user_login},
            )

    # ── toggles, rules, cooldowns ───────────────────────────────────────────
    async def set_module_toggle(
        self, channel_id: str, module: str, enabled: bool | None, actor: Actor
    ) -> None:
        async with transaction(self.conn):
            if enabled is None:
                await self.conn.execute(
                    "DELETE FROM module_toggles WHERE channel_id = ? AND module = ?", (channel_id, module)
                )
            else:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO module_toggles (channel_id, module, enabled) VALUES (?, ?, ?)",
                    (channel_id, module, int(enabled)),
                )
            await self._audit(actor, "module.toggle", channel_id, module, None, enabled)

    async def set_command_toggle(
        self,
        channel_id: str,
        command: str,
        actor: Actor,
        *,
        enabled: bool | None = None,
        log_level: str | None = None,
        clear_enabled: bool = False,
    ) -> None:
        async with transaction(self.conn):
            row = await self._one(
                "SELECT enabled, log_level FROM command_toggles WHERE channel_id = ? AND command = ?",
                (channel_id, command),
            )
            new_enabled = (
                None
                if clear_enabled
                else (enabled if enabled is not None else (row["enabled"] if row else None))
            )
            new_level = log_level if log_level is not None else (row["log_level"] if row else None)
            if new_enabled is None and new_level is None:
                await self.conn.execute(
                    "DELETE FROM command_toggles WHERE channel_id = ? AND command = ?", (channel_id, command)
                )
            else:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO command_toggles (channel_id, command, enabled, log_level) VALUES (?, ?, ?, ?)",
                    (channel_id, command, None if new_enabled is None else int(new_enabled), new_level),
                )
            await self._audit(
                actor,
                "command.toggle",
                channel_id,
                command,
                dict(row) if row else None,
                {"enabled": new_enabled, "log_level": new_level},
            )

    async def set_command_rule(
        self,
        channel_id: str,
        command: str,
        required_role: str | None,
        allowed_roles: list[str] | None,
        actor: Actor,
    ) -> None:
        async with transaction(self.conn):
            if required_role is None and allowed_roles is None:
                await self.conn.execute(
                    "DELETE FROM command_rules WHERE channel_id = ? AND command = ?", (channel_id, command)
                )
            else:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO command_rules (channel_id, command, required_role, allowed_roles) VALUES (?, ?, ?, ?)",
                    (
                        channel_id,
                        command,
                        required_role,
                        json.dumps(allowed_roles) if allowed_roles is not None else None,
                    ),
                )
            await self._audit(
                actor,
                "command.rule",
                channel_id,
                command,
                None,
                {"required_role": required_role, "allowed_roles": allowed_roles},
            )

    async def set_cooldown(
        self, channel_id: str, command: str, role: str, tier_s: int | None, user_s: int | None, actor: Actor
    ) -> None:
        async with transaction(self.conn):
            if tier_s is None or user_s is None:
                await self.conn.execute(
                    "DELETE FROM cooldown_rules WHERE channel_id = ? AND command = ? AND role = ?",
                    (channel_id, command, role),
                )
            else:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO cooldown_rules (channel_id, command, role, tier_s, user_s) VALUES (?, ?, ?, ?, ?)",
                    (channel_id, command, role, tier_s, user_s),
                )
            await self._audit(
                actor,
                "command.cooldown",
                channel_id,
                f"{command}:{role}",
                None,
                {"tier_s": tier_s, "user_s": user_s},
            )

    async def set_callback(
        self, channel_id: str, scope: str, kind: str, expr: str | None, syntax_version: str, actor: Actor
    ) -> None:
        async with transaction(self.conn):
            if expr is None:
                await self.conn.execute(
                    "DELETE FROM callbacks WHERE channel_id = ? AND scope = ? AND kind = ?",
                    (channel_id, scope, kind),
                )
            else:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO callbacks (channel_id, scope, kind, expr, syntax_version, updated_by, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (channel_id, scope, kind, expr, syntax_version, actor.user_id, now_ms()),
                )
            await self._audit(actor, "callback.set", channel_id, f"{scope}:{kind}", None, expr)

    async def set_ignored(
        self,
        channel_id: str,
        user_id: str,
        user_login: str,
        ignored: bool,
        actor: Actor,
        reason: str | None = None,
    ) -> None:
        async with transaction(self.conn):
            if ignored:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO ignore_list (channel_id, user_id, user_login, reason, added_by, added_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (channel_id, user_id, user_login, reason, actor.user_id, now_ms()),
                )
            else:
                await self.conn.execute(
                    "DELETE FROM ignore_list WHERE channel_id = ? AND user_id = ?", (channel_id, user_id)
                )
            await self._audit(
                actor,
                "ignore.add" if ignored else "ignore.remove",
                channel_id,
                user_id,
                None,
                {"login": user_login},
            )
