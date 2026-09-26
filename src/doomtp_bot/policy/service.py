"""PolicyService: the runtime's policy gate backed by the `bot` schema (ADR-0006).

Checks run against an in-memory snapshot; writes go through PolicyRepository and rebuild the snapshot.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

import structlog

from doomtp_bot.policy.cooldowns import CooldownTracker
from doomtp_bot.policy.repository import PolicyRepository
from doomtp_bot.policy.roles import (
    BOT_ADMIN_RANK,
    BOT_OWNER_RANK,
    BUILTIN_RANKS,
    GLOBAL,
    Role,
    roles_from_badges,
)
from doomtp_bot.policy.snapshot import ChannelSettings, IgnoreEntry, PolicySnapshot, load_snapshot
from doomtp_bot.runtime.context import ChannelInfo, Chatter
from doomtp_bot.runtime.policy import Decision
from doomtp_bot.runtime.result import Code
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, LogLevel
from doomtp_bot.storage.db import Connection

if TYPE_CHECKING:
    from doomtp_bot.runtime.context import ExecContext

log = structlog.get_logger(__name__)

CALLBACK_RATE_LIMIT_S = 30.0
CALLBACK_PRUNE_AT = 10_000


class _Bucket(NamedTuple):
    """One invocation's cooldown buckets: what a single look or claim works out once and uses throughout."""

    channel_id: str
    key: str
    tier: str
    user_id: str | None
    rule: Cooldown


class PolicyService:
    def __init__(
        self,
        conn: Connection,
        *,
        bot_owner_ids: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = PolicyRepository(conn)
        self.owners = bot_owner_ids
        self.cooldowns = CooldownTracker(clock)
        self._snapshot = PolicySnapshot()
        self._clock = clock
        self._callback_sent: dict[tuple[str, str, str, str], float] = {}
        self._write_lock = asyncio.Lock()

    async def reload(self) -> None:
        self._snapshot = await load_snapshot(self._repo.conn)

    async def mutate(self, operation: Callable[[PolicyRepository], object]) -> object:
        """Run a repository write and rebuild the snapshot."""
        async with self._write_lock:
            result = operation(self._repo)
            if asyncio.iscoroutine(result):
                result = await result
            await self.reload()
            return result

    # ── channels and chatters ───────────────────────────────────────────────
    def channels(self) -> list[ChannelSettings]:
        """Every channel the bot knows about, joined or parted, in no particular order."""
        return list(self._snapshot.channels.values())

    def channel_settings(self, channel_id: str) -> ChannelSettings | None:
        return self._snapshot.channels.get(channel_id)

    def channel_by_login(self, login: str) -> ChannelSettings | None:
        """A joined or parted channel by its login, as typed: any case, with or without a leading `#`."""
        return self._snapshot.channel_by_login(login)

    def channel_info(self, channel_id: str, login: str, **live: object) -> ChannelInfo:
        s = self._snapshot.channels.get(channel_id)
        if s is None:
            return ChannelInfo(id=channel_id, login=login, **live)  # type: ignore[arg-type]
        return ChannelInfo(
            id=channel_id,
            login=s.login,
            prefix=s.prefix,
            timezone=s.timezone,
            quiet_errors=s.quiet_errors,
            capabilities=s.capabilities,
            **live,  # type: ignore[arg-type]
        )

    def role_named(self, channel_id: str, name: str) -> Role | None:
        return self._snapshot.role_named(channel_id, name)

    def roles_in(self, channel_id: str) -> dict[str, Role]:
        """The custom roles defined in one scope (a channel, or `GLOBAL`) by name, without the other."""
        return dict(self._snapshot.roles_by_scope.get(channel_id, {}))

    async def members_of(self, role: Role) -> list[tuple[str, str | None, int | None]]:
        """(user_id, user_login, expires_at) for everyone holding the role, read from the database."""
        return await self._repo.members(role.id)

    def rank_of(self, channel_id: str, role_name: str) -> int | None:
        role = self._snapshot.role_named(channel_id, role_name)
        if role is not None:
            return role.rank
        return BUILTIN_RANKS.get(role_name)

    def build_chatter(
        self,
        channel_id: str,
        user_id: str,
        login: str,
        display: str = "",
        badges: frozenset[str] = frozenset(),
    ) -> Chatter:
        names: dict[str, int] = {"everyone": 0}
        for name in roles_from_badges(badges):
            names[name] = BUILTIN_RANKS[name]
        if user_id == channel_id:
            names["broadcaster"] = BUILTIN_RANKS["broadcaster"]
        for role in self._snapshot.custom_roles_for(channel_id, user_id):
            names[role.name] = role.rank
        if user_id in self._snapshot.global_admins:
            names["bot_admin"] = BOT_ADMIN_RANK
        if user_id in self.owners:
            names["bot_owner"] = BOT_OWNER_RANK
        ordered = tuple(sorted(names, key=lambda n: (-names[n], n)))
        return Chatter(
            id=user_id,
            login=login,
            display=display or login,
            badges=badges,
            roles=ordered,
            rank=max(names.values()),
        )

    def ignored_in(self, channel_id: str) -> frozenset[str]:
        """The user ids ignored in one scope (a channel, or `GLOBAL`), not counting the other."""
        return self._snapshot.ignored.get(channel_id, frozenset())

    def is_ignored(self, channel_id: str, user_id: str) -> bool:
        return user_id in self.ignored_in(channel_id) or user_id in self.ignored_in(GLOBAL)

    def ignore_entries(self, channel_id: str) -> list[IgnoreEntry]:
        """Who is ignored in one scope (a channel, or `GLOBAL`), with who did it and why."""
        return list(self._snapshot.ignore_entries.get(channel_id, {}).values())

    def ignore_entry(self, channel_id: str, user_id: str) -> IgnoreEntry | None:
        return self._snapshot.ignore_entries.get(channel_id, {}).get(user_id)

    def ignored_only_by_self(self, channel_id: str, user_id: str) -> bool:
        """Is this user ignored here, and only because they asked (`ignore me`)? Then the bot still hears
        their `unignore me`. An ignore anyone else set, here or everywhere, keeps them out."""
        entries = [e for scope in (channel_id, GLOBAL) if (e := self.ignore_entry(scope, user_id))]
        return bool(entries) and all(e.added_by == user_id for e in entries)

    # ── toggles (ADR-0006 §4) ───────────────────────────────────────────────
    def is_enabled(self, channel_id: str, spec: CommandSpec) -> bool:
        module, command = spec.module, spec.key
        if not spec.toggleable:
            return True
        toggles, commands = self._snapshot.module_toggles, self._snapshot.command_toggles
        if toggles.get((GLOBAL, module)) is False:
            return False
        if commands.get((GLOBAL, command)) is False:
            return False
        if (channel_id, command) in commands:
            return commands[(channel_id, command)]
        if (channel_id, module) in toggles:
            return toggles[(channel_id, module)]
        if (GLOBAL, module) in toggles:
            return toggles[(GLOBAL, module)]
        return True

    def switched_off_by(self, channel_id: str, spec: CommandSpec) -> str | None:
        """`"module"` or `"command"`: which toggle keeps this command off here, or None when it is on.
        For explain, which may say what chat won't (spec §6.6: off answers like unknown)."""
        if self.is_enabled(channel_id, spec):
            return None
        commands, key = self._snapshot.command_toggles, spec.key
        if commands.get((GLOBAL, key)) is False or commands.get((channel_id, key)) is False:
            return "command"
        return "module"

    def log_level(self, channel_id: str, spec: CommandSpec) -> LogLevel:
        levels = self._snapshot.command_log_levels
        level = levels.get((channel_id, spec.key)) or levels.get((GLOBAL, spec.key))
        return LogLevel(level) if level else spec.log_level

    # ── permission (ADR-0006 §1) ────────────────────────────────────────────
    def effective_rank(self, ctx: ExecContext) -> int:
        if ctx.run_as_rank is not None:
            return ctx.run_as_rank
        return ctx.invoker.rank if ctx.invoker else 0

    def required_role(self, channel_id: str, spec: CommandSpec) -> tuple[str, tuple[str, ...] | None]:
        rules = self._snapshot.command_rules
        rule = rules.get((channel_id, spec.key)) or rules.get((GLOBAL, spec.key))
        required = rule.required_role if rule and rule.required_role else spec.required_role
        allowed = rule.allowed_roles if rule else None
        return required, allowed

    def permission(self, ctx: ExecContext, spec: CommandSpec) -> Decision | None:
        if spec.fixed_policy:  # sentinels are always allowed (spec §8)
            return None
        channel_id = ctx.channel.id
        rank = self.effective_rank(ctx)
        required, allowed = self.required_role(channel_id, spec)
        if allowed and ctx.invoker is not None and set(allowed) & set(ctx.invoker.roles):
            return None
        required_rank = self.rank_of(channel_id, required)
        if required_rank is None:
            log.warning("policy.unknown_required_role", command=spec.name, role=required, channel=channel_id)
        elif rank >= required_rank:
            return None
        return Decision(
            False,
            Code.DENIED,
            f"requires {required}",
            {"command": spec.name, "required_role": required, "rank": rank},
        )

    # ── cooldowns (ADR-0006 §2) ─────────────────────────────────────────────
    def cooldown_rules(self, channel_id: str, spec: CommandSpec) -> dict[str, Cooldown]:
        """Configured cooldowns per role: spec defaults, then global rules, then this channel's rules."""
        merged: dict[str, Cooldown] = dict(spec.default_cooldowns)
        merged.update(self._snapshot.cooldown_rules.get((GLOBAL, spec.key), {}))
        merged.update(self._snapshot.cooldown_rules.get((channel_id, spec.key), {}))
        return merged

    def cooldown_rule(self, ctx: ExecContext, spec: CommandSpec) -> tuple[str, Cooldown] | None:
        """The rule of the highest-ranked role that has a rule and that the caller's rank reaches."""
        if spec.fixed_policy:  # sentinels never have cooldowns (spec §8)
            return None
        channel_id = ctx.channel.id
        merged = self.cooldown_rules(channel_id, spec)
        merged.setdefault("moderator", Cooldown(0, 0))
        rank = self.effective_rank(ctx)
        candidates = sorted(
            (
                (r, name, rule)
                for name, rule in merged.items()
                if (r := self.rank_of(channel_id, name)) is not None
            ),
            key=lambda c: (-c[0], c[1]),
        )
        for role_rank, name, rule in candidates:
            if role_rank <= rank:
                return name, rule
        return None

    def _cooldown_key(self, ctx: ExecContext, spec: CommandSpec) -> str:
        return f"{spec.key}@{ctx.trigger_id}" if ctx.trigger_id else spec.key

    def _bucket(self, ctx: ExecContext, spec: CommandSpec) -> _Bucket | None:
        """Which buckets this invocation uses, or None when its rule is zero or it has none."""
        found = self.cooldown_rule(ctx, spec)
        if found is None or (found[1].tier_s == 0 and found[1].user_s == 0):
            return None
        tier, rule = found
        user_id = ctx.invoker.id if ctx.invoker else None
        return _Bucket(ctx.channel.id, self._cooldown_key(ctx, spec), tier, user_id, rule)

    def _waiting(self, spec: CommandSpec, bucket: _Bucket | None) -> Decision | None:
        """The 128 refusal, carrying `{cooldown.*}`, if either bucket is still running. None means ready."""
        if bucket is None:
            return None
        state = self.cooldowns.state(bucket.channel_id, bucket.key, bucket.tier, bucket.user_id)
        if state.ready:
            return None
        return Decision(
            False,
            Code.COOLDOWN,
            "cooldown",
            {
                "command": spec.name,
                "tier": state.tier,
                "tier_remaining": state.tier_remaining,
                "user_remaining": state.user_remaining,
            },
        )

    # ── runtime.policy.Policy ───────────────────────────────────────────────
    def _gate(self, ctx: ExecContext, spec: CommandSpec) -> Decision | None:
        """Toggles, capabilities and permission: everything but cooldowns. None means allowed."""
        if not self.is_enabled(ctx.channel.id, spec):
            return Decision(False, Code.UNKNOWN, "disabled", {"command": spec.name})
        missing = set(spec.requires) - set(ctx.channel.capabilities)
        if missing:
            return Decision(
                False, Code.UNKNOWN, "unavailable", {"command": spec.name, "missing": sorted(missing)}
            )
        return self.permission(ctx, spec)

    def check(self, ctx: ExecContext, spec: CommandSpec) -> Decision:
        return self._gate(ctx, spec) or Decision.allow()

    def is_permitted(self, ctx: ExecContext, spec: CommandSpec) -> bool:
        return self._gate(ctx, spec) is None

    def check_cooldown(self, ctx: ExecContext, spec: CommandSpec) -> Decision:
        return self._waiting(spec, self._bucket(ctx, spec)) or Decision.allow()

    def claim_cooldown(self, ctx: ExecContext, spec: CommandSpec) -> Decision:
        bucket = self._bucket(ctx, spec)
        refused = self._waiting(spec, bucket)
        if refused is not None:
            return refused
        if bucket is not None:
            self.cooldowns.commit(bucket.channel_id, bucket.key, bucket.tier, bucket.user_id, bucket.rule)
        return Decision.allow()

    # ── callbacks (ADR-0006 §3) ─────────────────────────────────────────────
    def callback_expr(
        self, ctx: ExecContext, command: str | None, module: str | None, kind: str
    ) -> str | None:
        """Find a callback (command → module → channel, then the same globally) and apply its rate limit."""
        scopes = [
            s
            for s in (
                f"command:{command}" if command else None,
                f"module:{module}" if module else None,
                "channel",
            )
            if s
        ]
        callbacks = self._snapshot.callbacks
        expr = next(
            (e for ch in (ctx.channel.id, GLOBAL) for s in scopes if (e := callbacks.get((ch, s, kind)))),
            None,
        )
        if not expr:
            return None
        user = ctx.invoker.id if ctx.invoker else ""
        key = (ctx.channel.id, user, command or "", kind)
        now = self._clock()
        if now - self._callback_sent.get(key, -CALLBACK_RATE_LIMIT_S) < CALLBACK_RATE_LIMIT_S:
            return None
        if len(self._callback_sent) >= CALLBACK_PRUNE_AT:
            self._callback_sent = {
                k: t for k, t in self._callback_sent.items() if now - t < CALLBACK_RATE_LIMIT_S
            }
        self._callback_sent[key] = now
        return expr

    def reaches_setting_role(self, ctx: ExecContext, setting: str) -> bool:
        """Does the caller's rank reach the role named by a channel setting (e.g. var_admin_role)?"""
        settings = self.channel_settings(ctx.channel.id)
        role = getattr(settings, setting) if settings else "moderator"
        required = self.rank_of(ctx.channel.id, role)
        return required is not None and self.effective_rank(ctx) >= required
