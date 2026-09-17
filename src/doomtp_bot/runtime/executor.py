"""Dynamic semantics: evaluation, placeholder expansion, argument binding (spec §6–§7)."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from doomtp_bot import __version__
from doomtp_bot.lang.ast import And, Group, Invocation, Node, Or, Part, Pipe, Placeholder, Store, Text
from doomtp_bot.runtime.context import Args, CommandContext, ExecContext, RunCancelled
from doomtp_bot.runtime.namespaces import FieldPath, VarPath, classify
from doomtp_bot.runtime.result import MAX_DATA_BYTES, Code, Result
from doomtp_bot.runtime.values import (
    MISSING,
    ConversionError,
    convert,
    descend,
    is_missing,
    render,
    result_value,
)
from doomtp_bot.runtime.variables import VariableError, WriteOp, key_for

if TYPE_CHECKING:
    from doomtp_bot.runtime.policy import Policy
    from doomtp_bot.runtime.resolver import Resolved
    from doomtp_bot.runtime.spec import CommandSpec

log = structlog.get_logger(__name__)

STAGE_TIMEOUT_S = 3.0


class MissingValue(Exception):
    def __init__(self, reference: str) -> None:
        super().__init__(reference)
        self.reference = reference


@dataclass(frozen=True, slots=True)
class ScopeArgs:
    """Arguments of the enclosing custom command or trigger, for {arg.*} (spec §7.3)."""

    values: tuple[str, ...] = ()
    raw_text: str = ""
    raw_offsets: tuple[int, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_text(cls, text: str) -> ScopeArgs:
        """Split free text (e.g. a redemption input) on whitespace, remembering offsets for +raw."""
        values: list[str] = []
        offsets: list[int] = []
        i, n = 0, len(text)
        while i < n:
            while i < n and text[i].isspace():
                i += 1
            if i >= n:
                break
            start = i
            while i < n and not text[i].isspace():
                i += 1
            values.append(text[start:i])
            offsets.append(start)
        return cls(tuple(values), text, tuple(offsets))


class Scope:
    """One expression scope: numbered results and scope arguments (spec §6.4)."""

    def __init__(self, resolved: dict[int, Resolved], args: ScopeArgs | None = None) -> None:
        self.resolved = resolved
        self.args = args or ScopeArgs()
        self.results: dict[int, Result] = {}
        self.executed: list[int] = []


class Executor:
    def __init__(self, policy: Policy, stage_timeout: float = STAGE_TIMEOUT_S) -> None:
        self.policy = policy
        self.stage_timeout = stage_timeout

    async def run(self, node: Node, ctx: ExecContext, scope: Scope, stdin: Result | None = None) -> Result:
        return await self._eval(node, ctx, scope, prev=stdin, stdin=stdin)

    # ── §6.3 evaluation ─────────────────────────────────────────────────────
    async def _eval(
        self, node: Node, ctx: ExecContext, scope: Scope, prev: Result | None, stdin: Result | None
    ) -> Result:
        match node:
            case Invocation():
                return await self._invoke(node, ctx, scope, prev, stdin)
            case Pipe(left, right):
                r = await self._eval(left, ctx, scope, prev, stdin)
                return r if not r.ok else await self._eval(right, ctx, scope, r, r)
            case And(left, right):
                r = await self._eval(left, ctx, scope, prev, stdin)
                return r if not r.ok else await self._eval(right, ctx, scope, r, None)
            case Or(left, right):
                r = await self._eval(left, ctx, scope, prev, stdin)
                return r if r.ok else await self._eval(right, ctx, scope, r, None)
            case Group(inner):
                return await self._eval(inner, ctx, scope, prev, stdin)
            case Store(inner, target, append):
                r = await self._eval(inner, ctx, scope, prev, stdin)
                if not r.ok:
                    return r
                value = r.data if r.data is not None else r.message
                if value is None:
                    return r
                try:
                    key = key_for(ctx, target.namespace, target.name)
                    await ctx.variables.buffer(WriteOp("append" if append else "set", key, value))
                except VariableError as exc:
                    return exc.result()
                return r
        raise TypeError(f"not an AST node: {node!r}")

    async def _invoke(
        self, inv: Invocation, ctx: ExecContext, scope: Scope, prev: Result | None, stdin: Result | None
    ) -> Result:
        ctx.ensure_not_cancelled()
        command = scope.resolved[inv.index].command
        spec = command.spec
        try:
            values = tuple([await self.expand(arg, ctx, scope, prev) for arg in inv.args])
            params = await self.bind(spec, values, ctx)
        except MissingValue as exc:
            result = Result.failure(Code.USAGE, f"missing value: {exc.reference}")
        except _UsageError as exc:
            result = Result.failure(Code.USAGE, f"usage: {ctx.channel.prefix}{spec.usage()} — {exc}")
        else:
            self.policy.commit_cooldown(ctx, spec)
            args = Args(values, params, inv.raw_tail)
            cmd_ctx = CommandContext(ctx, inv.index, inv.name, prev)
            try:
                async with asyncio.timeout(self.stage_timeout):
                    result = await command.handler(cmd_ctx, args, stdin)
            except TimeoutError:
                result = Result.failure(Code.TIMEOUT, f"{inv.name} timed out")
            except (RunCancelled, asyncio.CancelledError):
                raise
            except Exception:
                log.exception("command.crashed", command=spec.name, run_id=ctx.run_id)
                result = Result.failure(Code.FAIL, f"{inv.name} failed")
            if result.data_size() > MAX_DATA_BYTES:
                result = Result.failure(Code.USAGE, f"{inv.name} produced too much data")
            scope.executed.append(inv.index)
        scope.results[inv.index] = result
        return result

    # ── §5.3 argument binding ───────────────────────────────────────────────
    async def bind(self, spec: CommandSpec, values: tuple[str, ...], ctx: ExecContext) -> dict[str, Any]:
        bound: dict[str, Any] = {}
        has_variadic = any(p.variadic for p in spec.params)
        if not has_variadic and len(values) > len(spec.params):
            raise _UsageError("too many arguments" if spec.params else "takes no arguments")
        for p in spec.params:
            i = p.index - 1
            raw: str | None
            if p.variadic:
                raw = " ".join(values[i:]) if len(values) > i else None
            else:
                raw = values[i] if i < len(values) else None
            if raw is None or (p.variadic and raw == ""):
                if p.required:
                    raise _UsageError(f"{p.name} is required")
                bound[p.name] = p.default
                continue
            try:
                bound[p.name] = await convert(
                    raw,
                    p.type,
                    choices=p.choices,
                    resolve_user=ctx.resolve_user,
                    minimum=p.min,
                    maximum=p.max,
                    max_len=p.max_len,
                )
            except ConversionError as exc:
                raise _UsageError(f"{p.name}: {exc}") from exc
        return bound

    # ── §7.3 expansion ──────────────────────────────────────────────────────
    async def expand(
        self, parts: tuple[Part, ...], ctx: ExecContext, scope: Scope, prev: Result | None
    ) -> str:
        out: list[str] = []
        for part in parts:
            if isinstance(part, Text):
                out.append(part.value)
            else:
                out.append(render(await self.placeholder_value(part, ctx, scope, prev)))
        return "".join(out)

    async def placeholder_value(
        self, ph: Placeholder, ctx: ExecContext, scope: Scope, prev: Result | None
    ) -> Any:
        value = await self.lookup(ph, ctx, scope, prev)
        if not is_missing(value) and ph.type is not None:
            try:
                value = await convert(
                    value, ph.type.name, choices=ph.type.choices, resolve_user=ctx.resolve_user
                )
            except ConversionError:
                value = MISSING
        if is_missing(value):
            if ph.fallback is not None:
                return await self.expand(ph.fallback, ctx, scope, prev)
            raise MissingValue("{" + ".".join((ph.root, *ph.path)) + "}")
        return value

    async def lookup(self, ph: Placeholder, ctx: ExecContext, scope: Scope, prev: Result | None) -> Any:
        root, path = ph.root, ph.path
        if root == "_":
            return MISSING if prev is None else result_value(prev, path)
        if root.isdigit():
            result = scope.results.get(int(root))
            if result is None or int(root) not in scope.executed:
                return MISSING
            return result_value(result, path)
        if root in ("chatter", "channel", "publisher"):
            target = classify(root, path)
            if isinstance(target, VarPath):
                try:
                    key = key_for(ctx, target.namespace, target.name)
                except VariableError:
                    return MISSING
                return descend(await ctx.variables.get(key), target.rest)
            if isinstance(target, FieldPath):
                return descend(self.field(ctx, target), target.rest)
            return MISSING
        if root in ("arg", "args"):
            return self.arg_value(scope.args, path if root == "arg" else ("1+", *path))
        if root == "event":
            return descend(ctx.event, path)
        if root == "match":
            return descend(ctx.match, path)
        if root == "cooldown":
            return descend(ctx.cooldown, path)
        if root == "denied":
            return descend(ctx.denied, path)
        if root == "bot":
            return descend({"name": "doomtp-bot", "id": "", "version": __version__, **ctx.bot}, path)
        if root == "run":
            return descend({"id": ctx.run_id, "trigger": ctx.trigger_type}, path)
        if root == "now":
            return descend(self.now_fields(ctx), path)
        if root == "cmd":
            pub = ctx.publisher
            if pub is None:
                return MISSING
            fields = {
                "name": pub.command_name,
                "alias": pub.alias,
                "id": pub.command_id,
                "version": pub.version,
                "owner": pub.login,
            }
            return descend(fields, path)
        return MISSING

    @staticmethod
    def arg_value(args: ScopeArgs, path: tuple[str, ...]) -> Any:
        head, *rest = path
        if head == "count":
            return len(args.values) if not rest else MISSING
        if head.endswith("+raw"):
            n = int(head[:-4])
            if n < 1 or n > len(args.values):
                return MISSING
            return (
                args.raw_text[args.raw_offsets[n - 1] :]
                if args.raw_offsets
                else " ".join(args.values[n - 1 :])
            )
        if head.endswith("+"):
            n = int(head[:-1])
            if n < 1 or n > len(args.values):
                return MISSING
            return " ".join(args.values[n - 1 :])
        if head.isdigit():
            n = int(head)
            return descend(args.values[n - 1], rest) if 1 <= n <= len(args.values) else MISSING
        if head in args.params:
            return descend(args.params[head], rest)
        return MISSING

    @staticmethod
    def field(ctx: ExecContext, target: FieldPath) -> Any:
        if target.root == "chatter":
            who = ctx.invoker
            if who is None:
                return MISSING
            return {
                "id": who.id,
                "name": who.login,
                "display": who.display or who.login,
                "rank": who.rank,
                "roles": list(who.roles),
                "is_sub": who.is_sub,
                "is_vip": who.is_vip,
                "is_mod": who.is_mod,
            }.get(target.field, MISSING)
        if target.root == "channel":
            ch = ctx.channel
            uptime = int(ctx.clock() - ch.started_at) if ch.live and ch.started_at else MISSING
            return {
                "id": ch.id,
                "name": ch.login,
                "display": ch.display or ch.login,
                "prefix": ch.prefix,
                "live": ch.live,
                "title": ch.title or MISSING,
                "game": ch.game or MISSING,
                "viewers": ch.viewers,
                "uptime": uptime,
            }.get(target.field, MISSING)
        pub = ctx.publisher
        if pub is None:
            return MISSING
        return {"id": pub.id, "name": pub.login, "display": pub.display or pub.login}.get(
            target.field, MISSING
        )

    @staticmethod
    def now_fields(ctx: ExecContext) -> dict[str, Any]:
        moment = ctx.now()
        with contextlib.suppress(ZoneInfoNotFoundError, ValueError):  # unknown zone (or no tzdata) → UTC
            moment = moment.astimezone(ZoneInfo(ctx.channel.timezone))
        return {
            "iso": moment.isoformat(timespec="seconds"),
            "unix": int(moment.timestamp()),
            "date": moment.date().isoformat(),
            "time": moment.strftime("%H:%M"),
            "weekday": moment.strftime("%A"),
        }


class _UsageError(Exception):
    pass
