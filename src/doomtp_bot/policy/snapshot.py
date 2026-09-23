"""Immutable in-memory view of all policy tables in the `bot` schema (ADR-0006 §5). Rebuilt after every write."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field

from doomtp_bot import clock
from doomtp_bot.lang.parser import DEFAULT_PREFIX
from doomtp_bot.policy.roles import GLOBAL, Role
from doomtp_bot.runtime.spec import Cooldown
from doomtp_bot.storage.db import Connection


@dataclass(frozen=True, slots=True)
class ChannelSettings:
    channel_id: str
    login: str
    active: bool = True
    status: str = "joined"
    tier: str = "basic"
    capabilities: frozenset[str] = frozenset()
    prefix: str = DEFAULT_PREFIX
    reply_hold_ms: int = 0
    log_enabled: bool = True
    history_backfill: bool = False
    quiet_errors: bool = False
    cc_edit_notice: bool = False  # say when a published command changed (ADR-0009)
    timezone: str = "UTC"
    automod_action: str = "off"  # off | delete | timeout (architecture §9.3)
    automod_timeout_s: int = 600
    channel_var_write_role: str = "moderator"
    grant_min_role: str = "moderator"
    publish_min_role: str = "moderator"
    create_min_role: str = "everyone"
    var_admin_role: str = "moderator"


@dataclass(frozen=True, slots=True)
class Membership:
    role_id: int
    expires_at: int | None  # ms epoch


@dataclass(frozen=True, slots=True)
class CommandRule:
    required_role: str | None
    allowed_roles: tuple[str, ...] | None


@dataclass(frozen=True)
class PolicySnapshot:
    channels: dict[str, ChannelSettings] = field(default_factory=dict)
    roles_by_id: dict[int, Role] = field(default_factory=dict)
    roles_by_scope: dict[str, dict[str, Role]] = field(
        default_factory=dict
    )  # channel_id|GLOBAL → name → Role
    memberships: dict[str, tuple[Membership, ...]] = field(default_factory=dict)  # user_id → memberships
    global_admins: frozenset[str] = frozenset()
    module_toggles: dict[tuple[str, str], bool] = field(default_factory=dict)
    command_toggles: dict[tuple[str, str], bool] = field(default_factory=dict)
    command_log_levels: dict[tuple[str, str], str] = field(default_factory=dict)
    command_rules: dict[tuple[str, str], CommandRule] = field(default_factory=dict)
    cooldown_rules: dict[tuple[str, str], dict[str, Cooldown]] = field(default_factory=dict)
    callbacks: dict[tuple[str, str, str], str] = field(default_factory=dict)  # (channel, scope, kind) → expr
    ignored: dict[str, frozenset[str]] = field(default_factory=dict)  # channel_id|GLOBAL → user_ids

    def role_named(self, channel_id: str, name: str) -> Role | None:
        return self.roles_by_scope.get(channel_id, {}).get(name) or self.roles_by_scope.get(GLOBAL, {}).get(
            name
        )

    def channel_by_login(self, login: str) -> ChannelSettings | None:
        """A joined or parted channel by its login, as typed: any case, with or without a leading `#`."""
        wanted = login.lower().lstrip("#")
        return next((c for c in self.channels.values() if c.login == wanted), None)

    def custom_roles_for(self, channel_id: str, user_id: str) -> list[Role]:
        now_ms = clock.now_ms()
        found: list[Role] = []
        for m in self.memberships.get(user_id, ()):
            if m.expires_at is not None and m.expires_at <= now_ms:
                continue
            role = self.roles_by_id.get(m.role_id)
            if role is not None and role.channel_id in (channel_id, GLOBAL):
                found.append(role)
        return found


async def load_snapshot(conn: Connection) -> PolicySnapshot:
    snap = PolicySnapshot()

    async with await conn.execute("SELECT * FROM channels") as cur:
        for r in await cur.fetchall():
            snap.channels[r["channel_id"]] = ChannelSettings(
                channel_id=r["channel_id"],
                login=r["login"],
                active=r["active"],
                status=r["status"],
                tier=r["tier"],
                capabilities=frozenset(json.loads(r["capabilities"] or "[]")),
                prefix=r["prefix"],
                reply_hold_ms=r["reply_hold_ms"],
                log_enabled=r["log_enabled"],
                history_backfill=r["history_backfill"],
                quiet_errors=r["quiet_errors"],
                cc_edit_notice=r["cc_edit_notice"],
                timezone=r["timezone"],
                automod_action=r["automod_action"],
                automod_timeout_s=r["automod_timeout_s"],
                channel_var_write_role=r["channel_var_write_role"],
                grant_min_role=r["grant_min_role"],
                publish_min_role=r["publish_min_role"],
                create_min_role=r["create_min_role"],
                var_admin_role=r["var_admin_role"],
            )

    async with await conn.execute("SELECT id, channel_id, name, rank, builtin FROM roles") as cur:
        for r in await cur.fetchall():
            role = Role(r["id"], r["channel_id"], r["name"], r["rank"], r["builtin"])
            snap.roles_by_id[role.id] = role
            snap.roles_by_scope.setdefault(role.channel_id, {})[role.name] = role

    memberships: dict[str, list[Membership]] = {}
    async with await conn.execute("SELECT role_id, user_id, expires_at FROM role_members") as cur:
        for r in await cur.fetchall():
            memberships.setdefault(r["user_id"], []).append(Membership(r["role_id"], r["expires_at"]))
    snap.memberships.update({k: tuple(v) for k, v in memberships.items()})

    async with await conn.execute("SELECT user_id FROM global_admins") as cur:
        admins = frozenset(r["user_id"] for r in await cur.fetchall())

    async with await conn.execute("SELECT channel_id, module, enabled FROM module_toggles") as cur:
        for r in await cur.fetchall():
            snap.module_toggles[(r["channel_id"], r["module"])] = r["enabled"]

    async with await conn.execute(
        "SELECT channel_id, command, enabled, log_level FROM command_toggles"
    ) as cur:
        for r in await cur.fetchall():
            if r["enabled"] is not None:
                snap.command_toggles[(r["channel_id"], r["command"])] = r["enabled"]
            if r["log_level"] is not None:
                snap.command_log_levels[(r["channel_id"], r["command"])] = r["log_level"]

    async with await conn.execute(
        "SELECT channel_id, command, required_role, allowed_roles FROM command_rules"
    ) as cur:
        for r in await cur.fetchall():
            allowed = tuple(json.loads(r["allowed_roles"])) if r["allowed_roles"] else None
            snap.command_rules[(r["channel_id"], r["command"])] = CommandRule(r["required_role"], allowed)

    async with await conn.execute(
        "SELECT channel_id, command, role, tier_s, user_s FROM cooldown_rules"
    ) as cur:
        for r in await cur.fetchall():
            snap.cooldown_rules.setdefault((r["channel_id"], r["command"]), {})[r["role"]] = Cooldown(
                r["tier_s"], r["user_s"]
            )

    async with await conn.execute("SELECT channel_id, scope, kind, expr FROM callbacks") as cur:
        for r in await cur.fetchall():
            snap.callbacks[(r["channel_id"], r["scope"], r["kind"])] = r["expr"]

    ignored: dict[str, set[str]] = {}
    async with await conn.execute("SELECT channel_id, user_id FROM ignore_list") as cur:
        for r in await cur.fetchall():
            ignored.setdefault(r["channel_id"], set()).add(r["user_id"])
    snap.ignored.update({k: frozenset(v) for k, v in ignored.items()})

    return dataclasses.replace(snap, global_admins=admins)
