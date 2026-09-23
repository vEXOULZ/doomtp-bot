"""`core_admin` module: chat commands that manage roles, permissions, cooldowns, toggles (ADR-0006).

Not toggleable. Every write goes through PolicyService (audited, snapshot rebuilt).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from doomtp_bot.lang import SYNTAX_VERSION
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import DEFAULT_PREFIX, Context, parse
from doomtp_bot.modules._common import actor, command_spec, need, policy_of, rank, user_arg
from doomtp_bot.policy.repository import PolicyRepository
from doomtp_bot.policy.roles import (
    BOT_ADMIN_RANK,
    BUILTIN_RANKS,
    CUSTOM_RANK_MAX,
    CUSTOM_RANK_MIN,
    GLOBAL,
    can_manage_role,
)
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, CommandRegistry, command
from doomtp_bot.runtime.result import Code, CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Example, LogLevel, Param
from doomtp_bot.runtime.values import ConversionError, convert

if TYPE_CHECKING:
    pass

MODULE = "core_admin"
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
PREFIX_FORBIDDEN = set('{}"\\|&>()')


def _registry(ctx: CommandContext) -> CommandRegistry:
    return ctx.service("registry")  # type: ignore[no-any-return]


def _is_broadcaster(ctx: CommandContext) -> bool:
    return ctx.invoker is not None and "broadcaster" in ctx.invoker.roles


async def _write(
    ctx: CommandContext, operation: Callable[[PolicyRepository], Any], scope: str | None = None
) -> Any:
    """Apply a policy write. Channel-scoped writes first create the channel row if it doesn't exist yet."""
    policy = policy_of(ctx)
    if scope != GLOBAL and policy.channel_settings(ctx.channel.id) is None:
        await policy.mutate(
            lambda repo: repo.ensure_channel(
                ctx.channel.id, ctx.channel.login, actor(ctx), ctx.channel.prefix
            )
        )
    return await policy.mutate(operation)


def _spec(name: str, summary: str, usage: str, required_role: str = "moderator", **kw: Any) -> CommandSpec:
    return CommandSpec(
        name=name,
        module=MODULE,
        toggleable=False,
        summary=summary,
        description=usage,
        params=(Param("1+", "arguments", description=usage),),
        required_role=required_role,
        log_level=LogLevel.INVOCATIONS,
        **kw,
    )


def _handler(fn: Any) -> Any:
    async def wrapped(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
        return await fn(ctx, list(args.values), args)  # type: ignore[no-any-return]

    return wrapped


def _int(raw: str, what: str, lo: int, hi: int) -> int:
    if not raw.lstrip("-").isdigit() or not lo <= int(raw) <= hi:
        raise CommandError(f"{what} must be a whole number from {lo} to {hi}")
    return int(raw)


# ── !role ───────────────────────────────────────────────────────────────────
ROLE_USAGE = "role list | create <name> <rank 1-99> | delete <name> | add <name> <user> [duration] | remove <name> <user> | who <name>"


@_handler
async def _role(ctx: CommandContext, v: list[str], args: Args) -> Result:
    policy = policy_of(ctx)
    need(v, 1, ROLE_USAGE)
    action, channel_id = v[0].lower(), ctx.channel.id
    if action == "list":
        roles = {
            **policy.roles_in(GLOBAL),
            **policy.roles_in(channel_id),
        }
        listing = ", ".join(f"{r.name} ({r.rank})" for r in sorted(roles.values(), key=lambda r: -r.rank))
        return Result.success(listing, [{"name": r.name, "rank": r.rank} for r in roles.values()])

    need(v, 2, ROLE_USAGE)
    name = v[1].lower()
    if action == "create":
        need(v, 3, ROLE_USAGE)
        new_rank = _int(v[2], "rank", CUSTOM_RANK_MIN, CUSTOM_RANK_MAX)
        if not ROLE_NAME_RE.match(name) or name in BUILTIN_RANKS:
            raise CommandError("role names: lowercase letters, digits, _ (not a built-in role)")
        if name in policy.roles_in(channel_id):
            raise CommandError(f"role {name} already exists")
        if not can_manage_role(
            rank(ctx), new_rank, actor_is_broadcaster=_is_broadcaster(ctx), role_is_channel=True
        ):
            return Result.failure(Code.FAIL, "you can only create roles ranked below your own")
        await _write(ctx, lambda repo: repo.create_role(channel_id, name, new_rank, actor(ctx)))
        return Result.success(f"created role {name} (rank {new_rank})")

    role = policy.role_named(channel_id, name)
    if role is None:
        raise CommandError(f"unknown role {name}")
    manageable = not role.builtin and can_manage_role(
        rank(ctx),
        role.rank,
        actor_is_broadcaster=_is_broadcaster(ctx),
        role_is_channel=role.channel_id == channel_id,
    )

    if action == "who":
        members = await policy.members_of(role)
        if not members:
            return Result.success(f"nobody has {name}", [])
        return Result.success(
            f"{name}: " + ", ".join(login or uid for uid, login, _ in members), [m[0] for m in members]
        )
    if action == "delete":
        if role.channel_id != channel_id or not manageable:
            return Result.failure(Code.FAIL, f"you can't delete {name}")
        await policy.mutate(lambda repo: repo.delete_role(role.id, actor(ctx)))
        return Result.success(f"deleted role {name}")
    if action in ("add", "remove"):
        need(v, 3, ROLE_USAGE)
        if not manageable:
            return Result.failure(Code.FAIL, f"you can't manage {name}")
        user = await user_arg(ctx, v[2])
        if action == "add":
            expires = None
            if len(v) > 3:
                try:
                    seconds = await convert(v[3], "duration")
                except ConversionError as exc:
                    raise CommandError(f"duration: {exc}") from exc
                expires = int((ctx.exec.clock() + seconds) * 1000)
            await policy.mutate(
                lambda repo: repo.add_member(
                    role.id, channel_id, name, user["id"], user["name"], expires, actor(ctx)
                )
            )
            return Result.success(f"gave {name} to {user['display']}" + (f" for {v[3]}" if expires else ""))
        removed = await policy.mutate(
            lambda repo: repo.remove_member(role.id, channel_id, name, user["id"], actor(ctx))
        )
        return Result.success(
            f"removed {name} from {user['display']}" if removed else f"{user['display']} doesn't have {name}"
        )
    raise CommandError(f"usage: {ROLE_USAGE}")


# ── !perm ───────────────────────────────────────────────────────────────────
PERM_USAGE = "perm show <command> | set <command> <role> | allow <command> <role,role> | clear <command>"


@_handler
async def _perm(ctx: CommandContext, v: list[str], args: Args) -> Result:
    policy = policy_of(ctx)
    need(v, 2, PERM_USAGE)
    action, spec, channel_id = v[0].lower(), command_spec(ctx, v[1]), ctx.channel.id
    if action == "show":
        required, allowed = policy.required_role(channel_id, spec)
        text = f"{spec.name}: requires {required}" + (
            f", or exactly: {', '.join(allowed)}" if allowed else ""
        )
        return Result.success(text, {"required_role": required, "allowed_roles": list(allowed or [])})
    if spec.fixed_policy:
        return Result.failure(Code.FAIL, f"{spec.name} is always allowed for everyone")
    if spec.module == MODULE and rank(ctx) < BOT_ADMIN_RANK:
        return Result.failure(Code.FAIL, "only bot admins can change admin command permissions")
    if action == "set":
        need(v, 3, PERM_USAGE)
        role = v[2].lower()
        if policy.rank_of(channel_id, role) is None:
            raise CommandError(f"unknown role {role}")
        await _write(ctx, lambda repo: repo.set_command_rule(channel_id, spec.name, role, None, actor(ctx)))
        return Result.success(f"{spec.name} now requires {role}")
    if action == "allow":
        need(v, 3, PERM_USAGE)
        roles = [r.strip().lower() for r in " ".join(v[2:]).split(",") if r.strip()]
        unknown = [r for r in roles if policy.rank_of(channel_id, r) is None]
        if not roles or unknown:
            raise CommandError(f"unknown role(s): {', '.join(unknown) or '(none given)'}")
        required, _ = policy.required_role(channel_id, spec)
        await _write(
            ctx, lambda repo: repo.set_command_rule(channel_id, spec.name, required, roles, actor(ctx))
        )
        return Result.success(f"{spec.name} is also allowed for: {', '.join(roles)}")
    if action == "clear":
        await policy.mutate(lambda repo: repo.set_command_rule(channel_id, spec.name, None, None, actor(ctx)))
        return Result.success(f"{spec.name} permissions reset to default ({spec.required_role})")
    raise CommandError(f"usage: {PERM_USAGE}")


# ── !cooldown ───────────────────────────────────────────────────────────────
COOLDOWN_USAGE = "cooldown show <command> | set <command> <role> <tier_s> <user_s> | clear <command> <role>"


@_handler
async def _cooldown(ctx: CommandContext, v: list[str], args: Args) -> Result:
    policy = policy_of(ctx)
    need(v, 2, COOLDOWN_USAGE)
    action, spec, channel_id = v[0].lower(), command_spec(ctx, v[1]), ctx.channel.id
    if action == "show":
        rules = policy.cooldown_rules(channel_id, spec)
        if not rules:
            return Result.success(f"{spec.name}: no cooldowns", {})
        text = "; ".join(f"{role} {c.tier_s}s/{c.user_s}s" for role, c in sorted(rules.items()))
        return Result.success(
            f"{spec.name} (shared/personal): {text}", {r: [c.tier_s, c.user_s] for r, c in rules.items()}
        )
    if spec.fixed_policy:
        return Result.failure(Code.FAIL, f"{spec.name} never has cooldowns")
    need(v, 3, COOLDOWN_USAGE)
    role = v[2].lower()
    if policy.rank_of(channel_id, role) is None:
        raise CommandError(f"unknown role {role}")
    if action == "set":
        need(v, 5, COOLDOWN_USAGE)
        tier_s, user_s = _int(v[3], "tier_s", 0, 86_400), _int(v[4], "user_s", 0, 86_400)
        await _write(
            ctx, lambda repo: repo.set_cooldown(channel_id, spec.name, role, tier_s, user_s, actor(ctx))
        )
        return Result.success(f"{spec.name} for {role}: {tier_s}s shared, {user_s}s personal")
    if action == "clear":
        await policy.mutate(
            lambda repo: repo.set_cooldown(channel_id, spec.name, role, None, None, actor(ctx))
        )
        return Result.success(f"{spec.name} cooldown for {role} reset to default")
    raise CommandError(f"usage: {COOLDOWN_USAGE}")


# ── !module / !cmd ──────────────────────────────────────────────────────────
MODULE_USAGE = "module list | enable|disable|reset <module> [global]"
CMD_USAGE = "cmd enable|disable|reset <command> [global] | log <command> <off|errors|output|invocations|all>"


async def _pack_modules(ctx: CommandContext) -> dict[str, CommandSpec]:
    """Pack names usable with !module here: what this channel published, plus global packs."""
    packs = ctx.exec.services.get("packs")
    if packs is None:
        return {}
    found: dict[str, CommandSpec] = {}
    for publication, pack in await packs.publications_in(ctx.channel.id, include_global=True):
        if publication.status == "active":
            found[pack.name] = CommandSpec(name="", module=pack.name, summary=pack.summary)
    return found


def _scope(ctx: CommandContext, v: list[str], position: int) -> str:
    if len(v) > position and v[position].lower() == "global":
        if rank(ctx) < BOT_ADMIN_RANK:
            raise CommandError("only bot admins can change global settings")
        return GLOBAL
    return ctx.channel.id


@_handler
async def _module(ctx: CommandContext, v: list[str], args: Args) -> Result:
    policy, registry = policy_of(ctx), _registry(ctx)
    need(v, 1, MODULE_USAGE)
    specs = {c.spec.module: c.spec for c in registry.all()}
    specs.update(await _pack_modules(ctx))  # packs published here toggle like any module (ADR-0012)
    modules = sorted(specs)
    action = v[0].lower()
    if action == "list":
        # A module is "on" when its toggle layers allow it; individual command overrides aren't shown here.
        states = {m: policy.is_enabled(ctx.channel.id, specs[m]) for m in modules}
        return Result.success(", ".join(f"{m} {'on' if on else 'off'}" for m, on in states.items()), states)
    need(v, 2, MODULE_USAGE)
    module = v[1].lower()
    if module not in modules:
        raise CommandError(f"unknown module {module}")
    if not specs[module].toggleable:
        return Result.failure(Code.FAIL, f"{module} can't be turned off")
    scope = _scope(ctx, v, 2)
    if action not in ("enable", "disable", "reset"):
        raise CommandError(f"usage: {MODULE_USAGE}")
    enabled = None if action == "reset" else action == "enable"
    await _write(ctx, lambda repo: repo.set_module_toggle(scope, module, enabled, actor(ctx)), scope)
    where = "everywhere" if scope == GLOBAL else "here"
    return Result.success(
        f"{module} {'reset' if enabled is None else ('enabled' if enabled else 'disabled')} {where}"
    )


@_handler
async def _cmd(ctx: CommandContext, v: list[str], args: Args) -> Result:
    need(v, 2, CMD_USAGE)
    action, spec = v[0].lower(), command_spec(ctx, v[1])
    if action == "log":
        need(v, 3, CMD_USAGE)
        try:
            level = LogLevel(v[2].lower())
        except ValueError as exc:
            raise CommandError(f"usage: {CMD_USAGE}") from exc
        await _write(
            ctx,
            lambda repo: repo.set_command_toggle(
                ctx.channel.id, spec.name, actor(ctx), log_level=level.value
            ),
        )
        return Result.success(f"{spec.name} log level: {level.value}")
    if not spec.toggleable:
        return Result.failure(Code.FAIL, f"{spec.name} can't be turned off")
    scope = _scope(ctx, v, 2)
    if action == "reset":
        await _write(
            ctx, lambda repo: repo.set_command_toggle(scope, spec.name, actor(ctx), clear_enabled=True), scope
        )
        return Result.success(f"{spec.name} reset")
    if action not in ("enable", "disable"):
        raise CommandError(f"usage: {CMD_USAGE}")
    enabled = action == "enable"
    await _write(
        ctx, lambda repo: repo.set_command_toggle(scope, spec.name, actor(ctx), enabled=enabled), scope
    )
    return Result.success(
        f"{spec.name} {'enabled' if enabled else 'disabled'}{' everywhere' if scope == GLOBAL else ''}"
    )


# ── !ignore / !prefix / !admin ──────────────────────────────────────────────
IGNORE_USAGE = "ignore list | add|remove <user> [global]"


@_handler
async def _ignore(ctx: CommandContext, v: list[str], args: Args) -> Result:
    policy = policy_of(ctx)
    need(v, 1, IGNORE_USAGE)
    action = v[0].lower()
    if action == "list":
        ids = sorted(policy.ignored_in(ctx.channel.id))
        return Result.success(f"{len(ids)} ignored here", ids)
    need(v, 2, IGNORE_USAGE)
    if action not in ("add", "remove"):
        raise CommandError(f"usage: {IGNORE_USAGE}")
    user, scope = await user_arg(ctx, v[1]), _scope(ctx, v, 2)
    await _write(
        ctx,
        lambda repo: repo.set_ignored(scope, user["id"], user["name"], action == "add", actor(ctx)),
        scope,
    )
    return Result.success(f"{'ignoring' if action == 'add' else 'no longer ignoring'} {user['display']}")


def validate_prefix(prefix: str) -> str | None:
    """Spec §2.1 prefix rules. Returns an error message or None."""
    if not 1 <= len(prefix) <= 3 or any(c.isspace() for c in prefix):
        return "prefix must be 1–3 characters without spaces"
    if prefix[0] in "/.":
        return "prefix can't start with / or ."
    if set(prefix) & PREFIX_FORBIDDEN:
        return "prefix can't contain { } \" \\ | & > ( )"
    if prefix[-1].isascii() and (prefix[-1].isalnum() or prefix[-1] in "_@-"):
        return "prefix can't end with a letter, digit, _, @ or -"
    return None


@_handler
async def _prefix(ctx: CommandContext, v: list[str], args: Args) -> Result:
    if not v:
        return Result.success(f"prefix: {ctx.channel.prefix}", ctx.channel.prefix)
    problem = validate_prefix(v[0])
    if problem:
        raise CommandError(problem)
    await _write(ctx, lambda repo: repo.set_channel_field(ctx.channel.id, "prefix", v[0], actor(ctx)))
    return Result.success(f"prefix is now {v[0]}")


ADMIN_USAGE = "admin add|remove <user>"


@_handler
async def _admin(ctx: CommandContext, v: list[str], args: Args) -> Result:
    policy = policy_of(ctx)
    need(v, 2, ADMIN_USAGE)
    action = v[0].lower()
    if action not in ("add", "remove"):
        raise CommandError(f"usage: {ADMIN_USAGE}")
    user = await user_arg(ctx, v[1])
    await policy.mutate(
        lambda repo: repo.set_global_admin(user["id"], user["name"], action == "add", actor(ctx))
    )
    return Result.success(f"{user['display']} is {'now' if action == 'add' else 'no longer'} a bot admin")


# ── !callback ───────────────────────────────────────────────────────────────
CALLBACK_USAGE = "callback set <on_cooldown|on_denied> <channel|module:name|command:name> <expression> | clear <kind> <scope>"
SCOPE_RE = re.compile(r"^(channel|module:[a-z0-9_]+|command:[a-z0-9][a-z0-9_-]*)$")


@_handler
async def _callback(ctx: CommandContext, v: list[str], args: Args) -> Result:
    policy = policy_of(ctx)
    need(v, 3, CALLBACK_USAGE)
    action, kind, scope = v[0].lower(), v[1].lower(), v[2].lower()
    if kind not in ("on_cooldown", "on_denied") or not SCOPE_RE.match(scope):
        raise CommandError(f"usage: {CALLBACK_USAGE}")
    if action == "clear":
        await policy.mutate(
            lambda repo: repo.set_callback(ctx.channel.id, scope, kind, None, SYNTAX_VERSION, actor(ctx))
        )
        return Result.success(f"cleared {kind} for {scope}")
    if action != "set" or not args.raw_tail:
        raise CommandError(f"usage: {CALLBACK_USAGE}")
    runtime = ctx.service("runtime")
    try:
        parse(args.raw_tail, Context.CALLBACK, runtime.parser_params(ctx.channel.prefix))
    except ParseError as exc:
        raise CommandError(str(exc)) from exc
    expr = args.raw_tail
    await _write(
        ctx, lambda repo: repo.set_callback(ctx.channel.id, scope, kind, expr, SYNTAX_VERSION, actor(ctx))
    )
    return Result.success(f"set {kind} for {scope}")


def _make(name: str, summary: str, usage: str, fn: Any, required_role: str = "moderator") -> Command:
    example = Example(DEFAULT_PREFIX + usage.split(" |")[0], "")
    return command(_spec(name, summary, usage, required_role, examples=(example,)))(fn)


COMMANDS: tuple[Command, ...] = (
    _make("role", "Manage custom roles", ROLE_USAGE, _role),
    _make("perm", "Change who can use a command", PERM_USAGE, _perm),
    _make("cooldown", "Change command cooldowns per role", COOLDOWN_USAGE, _cooldown),
    _make("module", "Turn command groups on or off", MODULE_USAGE, _module),
    _make("cmd", "Turn a command on or off, or set its log level", CMD_USAGE, _cmd),
    _make("ignore", "Ignore a user's commands", IGNORE_USAGE, _ignore),
    _make("prefix", "Show or change the command prefix", "prefix [new prefix]", _prefix),
    _make("admin", "Manage global bot admins", ADMIN_USAGE, _admin, required_role="bot_owner"),
    command(
        _spec("callback", "Customize replies for cooldowns and denials", CALLBACK_USAGE),
        raw_tail_subcommands=(("set", 4),),
    )(_callback),
)
