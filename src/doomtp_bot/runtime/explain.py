"""`!explain`: what would happen, and why (spec §9, architecture §4.4).

The report is built from the same parser, resolver and preflight a real run uses, so it can't drift from
what actually happens. `--run` evaluates too, with writes discarded and nothing sent.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from doomtp_bot.lang.ast import Node, invocations, stores, to_canonical
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import Context, parse
from doomtp_bot.runtime.namespaces import root_available
from doomtp_bot.runtime.preflight import placeholders_in, preflight
from doomtp_bot.runtime.result import Result

if TYPE_CHECKING:
    from doomtp_bot.runtime.context import ExecContext
    from doomtp_bot.runtime.engine import Runtime

MAX_SUMMARY_COMMANDS = 6


@dataclass(frozen=True, slots=True)
class InvocationReport:
    index: int
    name: str
    source: str = "unknown"  # builtin | publication | personal | unknown
    owner: str = ""
    version: int = 0
    required_role: str = ""
    rank: int = 0
    allowed: bool = False
    reason: str = ""  # why not, when not allowed
    cooldown_tier_s: float = 0.0
    cooldown_user_s: float = 0.0
    input_mode: str = "none"
    placeholders: tuple[dict[str, Any], ...] = ()
    grants: tuple[str, ...] = ()

    def summary(self) -> str:
        if self.allowed:
            waiting = max(self.cooldown_tier_s, self.cooldown_user_s)
            # Cooldowns don't fail preflight (spec 1.1): this one would fail with 128 when it is reached.
            return f"{self.index}:{self.name} ✓" + (f" (on cooldown, {waiting:.0f}s)" if waiting > 0 else "")
        return f"{self.index}:{self.name} ✗ {self.reason}"


@dataclass
class ExplainReport:
    expression: str
    context: Context = Context.LINE
    ast: str = ""
    invocations: list[InvocationReport] = field(default_factory=list)
    stores: list[dict[str, Any]] = field(default_factory=list)
    parse_error: str = ""
    failure: Result | None = None
    failed_index: int | None = None
    ran: bool = False
    run_result: Result | None = None
    executed: list[int] = field(default_factory=list)
    would_send: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The structured report the API returns (spec §9)."""
        return {
            "expression": self.expression,
            "context": str(self.context),
            "ast": self.ast,
            "parse_error": self.parse_error or None,
            "invocations": [dataclasses.asdict(i) for i in self.invocations],
            "stores": self.stores,
            "failure": (
                {"code": self.failure.code, "message": self.failure.message, "data": self.failure.data}
                if self.failure
                else None
            ),
            "failed_index": self.failed_index,
            "ran": self.ran,
            "result": (
                {"code": self.run_result.code, "message": self.run_result.message}
                if self.run_result
                else None
            ),
            "executed": self.executed,
            "would_send": self.would_send,
        }

    def one_line(self) -> str:
        """The compact chat answer."""
        if self.parse_error:
            return f"parse error: {self.parse_error}"
        parts = [i.summary() for i in self.invocations[:MAX_SUMMARY_COMMANDS]]
        if len(self.invocations) > MAX_SUMMARY_COMMANDS:
            parts.append(f"… +{len(self.invocations) - MAX_SUMMARY_COMMANDS} more")
        text = f"{self.ast} — " + ", ".join(parts)
        denied = list(dict.fromkeys(s["variable"] for s in self.stores if not s["allowed"]))
        if denied:  # a write that goes nowhere is the surprise explain exists to spare you (ADR-0010)
            text += f" — can't write {', '.join(denied)}"
        if self.failure is not None:
            where = f" at {self.failed_index}" if self.failed_index else ""
            text += f" — would fail{where}: {self.failure.message} (code {self.failure.code})"
        elif self.ran and self.run_result is not None:
            sent = self.would_send if self.would_send else "(nothing)"
            text += f" — ran: code {self.run_result.code}, would send: {sent}"
        else:
            text += " — would run"
        return text


async def explain(
    runtime: Runtime, text: str, ctx: ExecContext, *, context: Context = Context.LINE, run: bool = False
) -> ExplainReport:
    """Parse, resolve and preflight `text`, optionally running it with writes and sends disabled."""
    report = ExplainReport(expression=text, context=context)
    ctx = dataclasses.replace(ctx, context=context)  # checks run in the context being explained
    params = runtime.parser_params(ctx.channel.prefix)
    try:
        node = parse(text, context, params)
    except ParseError as exc:
        report.parse_error = str(exc)
        return report
    report.ast = to_canonical(node)

    resolver = runtime.resolver
    if runtime.custom is not None:
        resolver = await runtime.custom.resolver_for(ctx, node, resolver)

    pre = preflight(node, ctx, resolver, runtime.policy, ctx.variables.access, runtime.max_invocations)
    report.invocations = [
        _describe(inv, ctx, runtime, resolver, pre.resolved.get(inv.index)) for inv in invocations(node)
    ]
    report.stores = _stores(node, ctx, runtime)
    if not pre.ok:
        report.failure, report.failed_index = pre.result, pre.failed_index
        return report
    if run:
        await _dry_run(runtime, node, text, ctx, report, context)
    return report


def _describe(inv: Any, ctx: ExecContext, runtime: Runtime, resolver: Any, resolved: Any) -> InvocationReport:
    if resolved is None:
        resolved = resolver.resolve(ctx, inv)
    if resolved is None:
        return InvocationReport(inv.index, inv.name, reason="unknown command")
    spec = resolved.spec
    decision = runtime.policy.check(ctx, spec)
    required, _ = (
        runtime.policy.required_role(ctx.channel.id, spec)
        if hasattr(runtime.policy, "required_role")
        else (spec.required_role, None)
    )
    waiting = runtime.policy.check_cooldown(ctx, spec).info  # reported, not failed on (spec §5.2)
    custom = resolved.custom
    return InvocationReport(
        index=inv.index,
        name=inv.name,
        source=resolved.source,
        owner=custom.owner_login if custom else "",
        version=custom.version if custom else 0,
        required_role=required,
        rank=ctx.invoker.rank if ctx.invoker else 0,
        allowed=decision.allowed,
        reason="" if decision.allowed else (decision.reason or str(decision.code)),
        cooldown_tier_s=waiting.get("tier_remaining", 0.0),
        cooldown_user_s=waiting.get("user_remaining", 0.0),
        input_mode=str(spec.input),
        placeholders=tuple(
            {
                "reference": "{" + ".".join((ph.root, *ph.path)) + "}",
                "available": root_available(ph.root, ctx.context),
                "has_fallback": ph.fallback is not None,
            }
            for ph in placeholders_in(inv)
        ),
    )


def _stores(node: Node, ctx: ExecContext, runtime: Runtime) -> list[dict[str, Any]]:
    return [
        {
            "variable": f"{store.target.namespace}.{store.target.name}",
            "append": store.append,
            "allowed": ctx.variables.access.can_write(ctx, store.target.namespace, store.target.name),
        }
        for store in stores(node)
    ]


async def _dry_run(
    runtime: Runtime, node: Node, text: str, ctx: ExecContext, report: ExplainReport, context: Context
) -> None:
    """Run for real, then throw away everything it would have changed (spec §9)."""
    sub = runtime.make_context(
        channel=ctx.channel,
        invoker=ctx.invoker,
        context=context,
        trigger_type=ctx.trigger_type,
        run_as_rank=ctx.run_as_rank,
        rng=ctx.rng,
        clock=ctx.clock,
        bot=ctx.bot,
        dry_run=True,  # writes discarded, cooldowns untouched; the caller sends nothing
    )
    result = await runtime.run(text, sub)
    report.ran = True
    if result is not None:
        report.run_result, report.executed, report.would_send = result.result, result.executed, result.send
