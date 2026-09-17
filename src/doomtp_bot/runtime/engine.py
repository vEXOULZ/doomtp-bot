"""Runtime facade: text in → parse → preflight → execute → commit → output decision (ADR-0005)."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from doomtp_bot.lang.ast import Node
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import Context, NotACommand, ParserParams, parse, preprocess_line
from doomtp_bot.runtime.context import ChannelInfo, Chatter, ExecContext, Publisher, RunCancelled
from doomtp_bot.runtime.executor import Executor, Scope, ScopeArgs
from doomtp_bot.runtime.namespaces import is_reserved_var_name
from doomtp_bot.runtime.output import CallbackKind, Origin, decide_output
from doomtp_bot.runtime.policy import AllowAllPolicy, Decision, Policy
from doomtp_bot.runtime.preflight import MAX_INVOCATIONS, preflight
from doomtp_bot.runtime.registry import CommandRegistry
from doomtp_bot.runtime.resolver import BuiltinResolver, Resolver
from doomtp_bot.runtime.result import Code, Result
from doomtp_bot.runtime.values import UserResolver
from doomtp_bot.runtime.variables import (
    AllowAllAccess,
    InMemoryVariableStore,
    VariableAccess,
    VariableError,
    VariableSession,
    VariableStore,
    WriteOp,
)

log = structlog.get_logger(__name__)

EXPR_TIMEOUT_S = 6.0


@dataclass
class RunReport:
    expr: str
    result: Result
    origin: Origin
    send: str | None = None
    callback: CallbackKind | None = None
    decision: Decision | None = None
    failed_index: int | None = None
    failed_name: str | None = None
    executed: list[int] = field(default_factory=list)
    committed: list[WriteOp] = field(default_factory=list)
    cancelled: bool = False
    duration_ms: int = 0
    ast: Node | None = None


CommitHook = Callable[[ExecContext, list[WriteOp]], Awaitable[None]]


class Runtime:
    def __init__(
        self,
        registry: CommandRegistry,
        *,
        policy: Policy | None = None,
        resolver: Resolver | None = None,
        store: VariableStore | None = None,
        access: VariableAccess | None = None,
        resolve_user: UserResolver | None = None,
        on_commit: CommitHook | None = None,
        expr_timeout: float = EXPR_TIMEOUT_S,
        max_invocations: int = MAX_INVOCATIONS,
    ) -> None:
        self.registry = registry
        self.policy: Policy = policy or AllowAllPolicy()
        self.resolver: Resolver = resolver or BuiltinResolver(registry)
        self.store: VariableStore = store or InMemoryVariableStore()
        self.access: VariableAccess = access or AllowAllAccess()
        self.resolve_user = resolve_user
        self.on_commit = on_commit
        self.expr_timeout = expr_timeout
        self.max_invocations = max_invocations
        self.executor = Executor(self.policy)

    # ── context construction ────────────────────────────────────────────────
    def make_context(
        self,
        *,
        channel: ChannelInfo,
        invoker: Chatter | None,
        context: Context = Context.LINE,
        **kwargs: Any,
    ) -> ExecContext:
        kwargs.setdefault("resolve_user", self.resolve_user)
        return ExecContext(
            context=context,
            channel=channel,
            invoker=invoker,
            variables=VariableSession(self.store, self.access),
            **kwargs,
        )

    def parser_params(self, prefix: str) -> ParserParams:
        return ParserParams(
            prefix=prefix, raw_tail_from=self.registry.raw_tail_from, reserved_var_names=is_reserved_var_name
        )

    # ── entry point ─────────────────────────────────────────────────────────
    async def run(
        self,
        text: str,
        ctx: ExecContext,
        *,
        reply_parent_login: str | None = None,
        scope_args: ScopeArgs | None = None,
        publisher: Publisher | None = None,
        stdin: Result | None = None,
    ) -> RunReport | None:
        """Run an expression. Returns None only for Line-context text that isn't a command."""
        started = time.monotonic()
        if publisher is not None:
            ctx.publisher = publisher
        if ctx.context is Context.LINE:
            text = preprocess_line(text, reply_parent_login)
        try:
            node = parse(text, ctx.context, self.parser_params(ctx.channel.prefix))
        except NotACommand:
            return None
        except ParseError as exc:
            visible = self._first_command_permitted(text, ctx)
            report = RunReport(text, Result.failure(Code.USAGE, str(exc)), "parse")
            report.send = decide_output(
                report.result,
                origin="parse",
                context=ctx.context,
                quiet_errors=ctx.channel.quiet_errors,
                parse_error_visible=visible,
            ).send
            return self._finish(report, started)

        pre = preflight(node, ctx, self.resolver, self.policy, ctx.variables.access, self.max_invocations)
        if not pre.ok:
            assert pre.result is not None
            report = RunReport(
                text,
                pre.result,
                "preflight",
                decision=pre.decision,
                failed_index=pre.failed_index,
                failed_name=pre.failed_name,
                ast=node,
            )
            self._decide(report, ctx)
            return self._finish(report, started)

        scope = Scope(pre.resolved, scope_args)
        report = RunReport(text, Result(), "runtime", ast=node)
        try:
            async with asyncio.timeout(self.expr_timeout):
                report.result = await self.executor.run(node, ctx, scope, stdin)
        except RunCancelled:
            ctx.variables.discard()
            report.result = Result.failure(Code.CANCELLED, "cancelled by moderation")
            report.cancelled = True
        except TimeoutError:
            ctx.variables.discard()
            report.result = Result.failure(Code.TIMEOUT, "timed out")
        else:
            try:
                report.committed = await ctx.variables.commit(ctx)
            except VariableError as exc:
                log.warning("variables.commit_failed", run_id=ctx.run_id, error=exc.message)
                report.result = exc.result()
            if report.committed and self.on_commit is not None:
                await self.on_commit(ctx, report.committed)
        report.executed = list(scope.executed)
        self._decide(report, ctx)
        return self._finish(report, started)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _decide(self, report: RunReport, ctx: ExecContext) -> None:
        decision = decide_output(
            report.result,
            origin=report.origin,
            context=ctx.context,
            failed_index=report.failed_index,
            quiet_errors=ctx.channel.quiet_errors,
        )
        report.send, report.callback = decision.send, decision.callback

    @staticmethod
    def _finish(report: RunReport, started: float) -> RunReport:
        report.duration_ms = int((time.monotonic() - started) * 1000)
        return report

    def _first_command_permitted(self, text: str, ctx: ExecContext) -> bool:
        """§3.4: parse errors are shown only if the first command resolves and the invoker may run it."""
        prefix = re.escape(ctx.channel.prefix)
        match = re.match(rf"^(?:\(\s+)*(?:{prefix})?(@?)([A-Za-z0-9][A-Za-z0-9_-]*)", text)
        if match is None:
            return False
        resolved = self.resolver.resolve_name(ctx, match.group(2).lower(), personal=bool(match.group(1)))
        return resolved is not None and self.policy.is_permitted(ctx, resolved.command.spec)
