"""Static semantics: resolution and all-or-nothing preflight (spec §5).

Cooldowns are not checked here since spec 1.1 — they fail the individual invocation at runtime.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from doomtp_bot.lang.ast import (
    And,
    Expr,
    Group,
    IfElse,
    Invocation,
    Node,
    Or,
    Pipe,
    Placeholder,
    Ref,
    Store,
    Subst,
    arg_exprs,
    arg_substitutions,
    invocations,
    render_expr,
    substitutions,
    walk_expr,
)
from doomtp_bot.lang.parser import Context
from doomtp_bot.runtime.context import Publisher
from doomtp_bot.runtime.namespaces import bot_field_known, root_available
from doomtp_bot.runtime.result import Code, Result, error_result
from doomtp_bot.runtime.spec import InputMode
from doomtp_bot.runtime.variables import VariableError, key_for

if TYPE_CHECKING:
    from doomtp_bot.runtime.context import ExecContext
    from doomtp_bot.runtime.policy import Policy
    from doomtp_bot.runtime.resolver import Resolved, Resolver
    from doomtp_bot.runtime.variables import VariableAccess

MAX_INVOCATIONS = 8
MAX_CC_DEPTH = 3


@dataclass
class Preflight:
    ok: bool
    resolved: dict[int, Resolved] = field(default_factory=dict)
    # Resolutions inside each expanded custom command body, keyed by command id (ADR-0009).
    bodies: dict[str, dict[int, Resolved]] = field(default_factory=dict)
    result: Result | None = None
    failed_index: int | None = None
    failed_name: str | None = None


def first_invocations(node: Node) -> list[Invocation]:
    """The invocations that can run first in `node`: an `ifelse` starts with either branch."""
    match node:
        case Invocation():
            return [node]
        case And(left, _) | Or(left, _) | Pipe(left, _):
            return first_invocations(left)
        case Group(inner) | Store(inner, _, _):
            return first_invocations(inner)
        case IfElse(_, then, else_):
            return first_invocations(then) + (first_invocations(else_) if else_ is not None else [])
    raise TypeError(f"not an AST node: {node!r}")


def stdin_receivers(node: Node) -> set[int]:
    """Invocation indexes that receive pipe stdin (the first evaluated invocation of each pipe's right side)."""
    found: set[int] = set()

    def walk(n: Node) -> None:
        match n:
            case Pipe(left, right):
                found.update(inv.index for inv in first_invocations(right))
                walk(left)
                walk(right)
            case And(left, right) | Or(left, right):
                walk(left)
                walk(right)
            case Group(inner) | Store(inner, _, _):
                walk(inner)
            case IfElse(_, then, else_):
                walk(then)
                if else_ is not None:
                    walk(else_)
            case Invocation():
                pass

    walk(node)
    return found


def placeholders_in(invocation: Invocation) -> Iterator[Placeholder]:
    """Every placeholder an invocation's arguments hold, fallbacks' and nested ones included."""
    for top in arg_exprs(invocation):
        for expr in walk_expr(top):
            if isinstance(expr, Placeholder):
                yield expr


def refs_in(expr: Expr) -> Iterator[Ref]:
    for e in walk_expr(expr):
        if isinstance(e, Ref):
            yield e


def check_expr(expr: Expr, index: int, ctx: ExecContext) -> tuple[str, str] | None:
    """(problem, reference) for the first name not valid here, else None (spec §5.2 check 4, §7.2).

    `index` is the invocation the expression belongs to, or for a `{!…}` the invocation holding it: that
    decides which `_N` have run. Every problem is reported as E_BAD_REFERENCE.
    """
    for ref in refs_in(expr):
        reference = "{" + render_expr(ref) + "}"
        root = ref.root
        if not root_available(root, ctx.context):
            return f"{{{root}}} is not available here", reference
        if root.startswith("_") and root != "_" and int(root[1:]) >= index:
            return f"{reference} refers to a command that runs later", reference
        if root.startswith("$") and not bot_field_known(root, ref.path[0]):
            return f"{root} has no field {ref.path[0]}", reference
    return None


def exprs_of(invocation: Invocation) -> list[Expr]:
    return list(arg_exprs(invocation))


def preflight(
    node: Node,
    ctx: ExecContext,
    resolver: Resolver,
    policy: Policy,
    access: VariableAccess,
) -> Preflight:
    """Check the whole AST, including custom command bodies, before anything runs (spec §5.2).

    Bodies are expanded with the invoker's identity and the owner as publisher, so an inner command is
    checked against the person who typed it, never against the author (ADR-0009).
    """
    outcome = Preflight(ok=True)
    counted = 0

    def fail(index: int | None, name: str | None, result: Result) -> Preflight:
        return Preflight(False, outcome.resolved, outcome.bodies, result, index, name)

    def check_invocation(
        inv: Invocation,
        here: ExecContext,
        resolved_map: dict[int, Resolved],
        receives_stdin: set[int],
        stack: tuple[str, ...],
        holder_index: int | None = None,
    ) -> Preflight | None:
        nonlocal counted
        counted += 1
        if counted > MAX_INVOCATIONS:
            return fail(
                None,
                None,
                error_result("E_TOO_MANY", f"too many commands (max {MAX_INVOCATIONS})", max=MAX_INVOCATIONS),
            )
        resolved = resolver.resolve(here, inv)
        if resolved is None:
            return fail(inv.index, inv.name, Result.failure(Code.UNKNOWN, f"unknown command: {inv.name}"))
        resolved_map[inv.index] = resolved
        spec = resolved.spec
        decision = policy.check(here, spec)
        if not decision.allowed:
            message = "permission denied" if decision.code == Code.DENIED else f"unknown command: {inv.name}"
            return fail(inv.index, inv.name, Result.failure(decision.code, message, dict(decision.info)))
        if spec.input is InputMode.NONE and inv.index in receives_stdin:
            return fail(
                inv.index,
                inv.name,
                error_result(
                    "E_INPUT_NOT_ACCEPTED", f"{inv.name} does not accept piped input", command=inv.name
                ),
            )
        holder = inv.index if holder_index is None else holder_index
        failure = check_exprs(inv, exprs_of(inv), holder, here)
        if failure is not None:
            return failure
        for inner in substitutions(iter(exprs_of(inv))):
            failure = check_invocation(inner, here, resolved_map, receives_stdin, stack, holder)
            if failure is not None:
                return failure
        if resolved.custom is not None:
            return check_body(inv, resolved, here, stack)
        return None

    def check_exprs(
        inv: Invocation | None, exprs: list[Expr], index: int, here: ExecContext
    ) -> Preflight | None:
        for expr in exprs:
            found = check_expr(expr, index, here)
            if found is not None:
                problem, reference = found
                return fail(
                    inv.index if inv else None,
                    inv.name if inv else None,
                    error_result("E_BAD_REFERENCE", problem, reference=reference),
                )
        return None

    def check_cond(
        cond: IfElse, here: ExecContext, resolved_map: dict[int, Resolved], stack: tuple[str, ...]
    ) -> Preflight | None:
        """`ifelse`'s condition runs before either branch, so only commands before it have results."""
        first = first_invocations(cond.then)[0].index
        failure = check_exprs(None, [p for p in cond.cond if isinstance(p, Placeholder)], first, here)
        if failure is not None:
            return failure
        for inner in arg_substitutions(cond.cond):
            failure = check_invocation(inner, here, resolved_map, set(), stack, first)
            if failure is not None:
                return failure
        return None

    def check_body(
        inv: Invocation, resolved: Resolved, here: ExecContext, stack: tuple[str, ...]
    ) -> Preflight | None:
        target = resolved.custom
        assert target is not None
        if target.command_id in stack:
            return fail(
                inv.index, inv.name, error_result("E_CC_CYCLE", f"{inv.name} calls itself", command=inv.name)
            )
        if len(stack) + 1 > MAX_CC_DEPTH and not target.system:  # a sentinel is never too deep
            return fail(
                inv.index,
                inv.name,
                error_result(
                    "E_CC_DEPTH", f"custom commands nested deeper than {MAX_CC_DEPTH}", max=MAX_CC_DEPTH
                ),
            )
        body_ctx = dataclasses.replace(
            here,
            context=Context.BODY,
            publisher=Publisher(
                id=target.owner_id,
                login=target.owner_login,
                command_id=target.command_id,
                command_name=target.name,
                alias=inv.name,
                version=target.version,
                publication=target.publication,
                pack_id=target.pack_id,
            ),
        )
        resolved_map = outcome.bodies.setdefault(target.command_id, {})
        if resolved_map:  # already expanded through another invocation of the same command
            return None
        return walk(
            target.body, body_ctx, resolved_map, stdin_receivers(target.body), (*stack, target.command_id)
        )

    def check_store(store: Store, here: ExecContext) -> Preflight | None:
        target = store.target
        for step in target.path:
            if any(isinstance(e, Subst) for e in walk_expr(step)):
                return fail(
                    None,
                    None,
                    error_result(
                        "E_BAD_REFERENCE", "a store target can't run {!…}", reference=render_expr(target)
                    ),
                )
        after = max(inv.index for inv in invocations(store.inner)) + 1  # the path is read once inner ran
        failure = check_exprs(None, list(target.path), after, here)
        if failure is not None:
            return failure
        try:
            key_for(here, target.namespace, target.name)  # same addressability rules as the write itself
        except VariableError as exc:
            return fail(None, None, exc.result())
        if not access.can_write(here, target.namespace, target.name):
            variable = f"{target.namespace}.{target.name}"
            return fail(
                None,
                None,
                Result.failure(Code.DENIED, f"not allowed to write {variable}", {"variable": variable}),
            )
        return None

    def walk(
        n: Node,
        here: ExecContext,
        resolved_map: dict[int, Resolved],
        receives_stdin: set[int],
        stack: tuple[str, ...],
    ) -> Preflight | None:
        def sub(x: Node) -> Preflight | None:
            return walk(x, here, resolved_map, receives_stdin, stack)

        match n:
            case Invocation():
                return check_invocation(n, here, resolved_map, receives_stdin, stack)
            case And(left, right) | Or(left, right) | Pipe(left, right):
                return sub(left) or sub(right)
            case Group(inner):
                return sub(inner)
            case Store(inner, _, _):
                return sub(inner) or check_store(n, here)
            case IfElse(_, then, else_):
                return (
                    check_cond(n, here, resolved_map, stack)
                    or sub(then)
                    or (sub(else_) if else_ is not None else None)
                )
        return None

    failure = walk(node, ctx, outcome.resolved, stdin_receivers(node), ())
    return failure if failure is not None else outcome
