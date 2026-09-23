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
    Group,
    Invocation,
    Node,
    Or,
    Pipe,
    Placeholder,
    Store,
    Text,
    invocations,
)
from doomtp_bot.lang.parser import Context
from doomtp_bot.runtime.context import Publisher
from doomtp_bot.runtime.namespaces import VarPath, classify, is_reserved_var_name, root_available
from doomtp_bot.runtime.policy import Decision
from doomtp_bot.runtime.result import Code, Result, error_result
from doomtp_bot.runtime.spec import InputMode
from doomtp_bot.runtime.variables import VAR_NAME_RE, VariableError, key_for

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
    decision: Decision | None = None


def stdin_receivers(node: Node) -> set[int]:
    """Invocation indexes that receive pipe stdin (the first evaluated invocation of each pipe's right side)."""
    found: set[int] = set()

    def walk(n: Node) -> None:
        match n:
            case Pipe(left, right):
                found.add(invocations(right)[0].index)
                walk(left)
                walk(right)
            case And(left, right) | Or(left, right):
                walk(left)
                walk(right)
            case Group(inner) | Store(inner, _, _):
                walk(inner)
            case Invocation():
                pass

    walk(node)
    return found


def placeholders_in(invocation: Invocation) -> Iterator[Placeholder]:
    def walk(parts: tuple[Text | Placeholder, ...]) -> Iterator[Placeholder]:
        for part in parts:
            if isinstance(part, Placeholder):
                yield part
                if part.fallback:
                    yield from walk(part.fallback)

    for arg in invocation.args:
        yield from walk(arg)


def check_placeholder(ph: Placeholder, index: int, ctx: ExecContext) -> str | None:
    """Return an error message, or None if the reference is valid here (spec §5.2 check 4, §7.2).

    Every message here is reported as E_BAD_REFERENCE.
    """
    root, path = ph.root, ph.path
    if not root_available(root, ctx.context):
        return f"{{{root}}} is not available here"
    if root.isdigit():
        if int(root) >= index:
            return f"{{{root}}} refers to a command that runs later"
    elif root in ("chatter", "channel", "publisher"):
        target = classify(root, path)
        if target is None:
            return f"incomplete reference {{{'.'.join((root, *path))}}}"
        if isinstance(target, VarPath) and (
            not VAR_NAME_RE.match(target.name) or is_reserved_var_name(target.namespace, target.name)
        ):
            return f"invalid variable name {target.namespace}.{target.name}"
    elif root == "arg":
        if not path:
            return "{arg} needs a position, e.g. {arg.1}"
        for i, segment in enumerate(path):
            if segment.endswith(("+", "+raw")) and i != 0:
                return "{arg.N+} captures must come right after arg"
    elif root in ("bot", "now", "run", "cmd") and not path:
        return f"{{{root}}} needs a field, e.g. {{{root}.name}}"
    return None


def preflight(
    node: Node,
    ctx: ExecContext,
    resolver: Resolver,
    policy: Policy,
    access: VariableAccess,
    max_invocations: int = MAX_INVOCATIONS,
    max_depth: int = MAX_CC_DEPTH,
) -> Preflight:
    """Check the whole AST, including custom command bodies, before anything runs (spec §5.2).

    Bodies are expanded with the invoker's identity and the owner as publisher, so an inner command is
    checked against the person who typed it, never against the author (ADR-0009).
    """
    outcome = Preflight(ok=True)
    counted = 0

    def fail(
        index: int | None, name: str | None, result: Result, decision: Decision | None = None
    ) -> Preflight:
        return Preflight(False, outcome.resolved, outcome.bodies, result, index, name, decision)

    def check_invocation(
        inv: Invocation,
        here: ExecContext,
        resolved_map: dict[int, Resolved],
        receives_stdin: set[int],
        stack: tuple[str, ...],
    ) -> Preflight | None:
        nonlocal counted
        counted += 1
        if counted > max_invocations:
            return fail(
                None,
                None,
                error_result("E_TOO_MANY", f"too many commands (max {max_invocations})", max=max_invocations),
            )
        resolved = resolver.resolve(here, inv)
        if resolved is None:
            return fail(inv.index, inv.name, Result.failure(Code.UNKNOWN, f"unknown command: {inv.name}"))
        resolved_map[inv.index] = resolved
        spec = resolved.spec
        decision = policy.check(here, spec)
        if not decision.allowed:
            message = "permission denied" if decision.code == Code.DENIED else f"unknown command: {inv.name}"
            return fail(inv.index, inv.name, Result.failure(decision.code, message), decision)
        if spec.input is InputMode.NONE and inv.index in receives_stdin:
            return fail(
                inv.index,
                inv.name,
                error_result(
                    "E_INPUT_NOT_ACCEPTED", f"{inv.name} does not accept piped input", command=inv.name
                ),
            )
        for ph in placeholders_in(inv):
            problem = check_placeholder(ph, inv.index, here)
            if problem is not None:
                reference = "{" + ".".join((ph.root, *ph.path)) + "}"
                return fail(
                    inv.index, inv.name, error_result("E_BAD_REFERENCE", problem, reference=reference)
                )
        if resolved.custom is not None:
            return check_body(inv, resolved, here, stack)
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
        if len(stack) + 1 > max_depth:
            return fail(
                inv.index,
                inv.name,
                error_result("E_CC_DEPTH", f"custom commands nested deeper than {max_depth}", max=max_depth),
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
        try:
            key_for(here, target.namespace, target.name)  # same addressability rules as the write itself
        except VariableError as exc:
            return fail(None, None, exc.result())
        if not access.can_write(here, target.namespace, target.name):
            return fail(
                None,
                None,
                Result.failure(Code.DENIED, f"not allowed to write {target.namespace}.{target.name}"),
                Decision(
                    False,
                    Code.DENIED,
                    "variable write denied",
                    {"variable": f"{target.namespace}.{target.name}"},
                ),
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
        return None

    failure = walk(node, ctx, outcome.resolved, stdin_receivers(node), ())
    return failure if failure is not None else outcome
