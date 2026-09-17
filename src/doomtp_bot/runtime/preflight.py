"""Static semantics: resolution and all-or-nothing preflight (spec §5)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from doomtp_bot.lang.ast import And, Group, Invocation, Node, Or, Pipe, Placeholder, Store, Text, invocations
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


@dataclass
class Preflight:
    ok: bool
    resolved: dict[int, Resolved] = field(default_factory=dict)
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
    """Return an error message, or None if the reference is valid here (spec §5.2 check 5, §7.2).

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
) -> Preflight:
    outcome = Preflight(ok=True)
    receives_stdin = stdin_receivers(node)

    def fail(
        index: int | None, name: str | None, result: Result, decision: Decision | None = None
    ) -> Preflight:
        return Preflight(False, outcome.resolved, result, index, name, decision)

    def check_invocation(inv: Invocation) -> Preflight | None:
        resolved = resolver.resolve(ctx, inv)
        if resolved is None:
            return fail(inv.index, inv.name, Result.failure(Code.UNKNOWN, f"unknown command: {inv.name}"))
        outcome.resolved[inv.index] = resolved
        spec = resolved.command.spec
        decision = policy.check(ctx, spec)
        if not decision.allowed:
            messages = {Code.DENIED: "permission denied", Code.COOLDOWN: "on cooldown"}
            message = messages.get(decision.code, f"unknown command: {inv.name}")  # type: ignore[call-overload]
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
            problem = check_placeholder(ph, inv.index, ctx)
            if problem is not None:
                reference = "{" + ".".join((ph.root, *ph.path)) + "}"
                return fail(
                    inv.index, inv.name, error_result("E_BAD_REFERENCE", problem, reference=reference)
                )
        return None

    def check_store(store: Store) -> Preflight | None:
        target = store.target
        try:
            key_for(ctx, target.namespace, target.name)  # same addressability rules as the write itself
        except VariableError as exc:
            return fail(None, None, exc.result())
        if not access.can_write(ctx, target.namespace, target.name):
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

    def walk(n: Node) -> Preflight | None:
        match n:
            case Invocation():
                return check_invocation(n)
            case And(left, right) | Or(left, right) | Pipe(left, right):
                return walk(left) or walk(right)
            case Group(inner):
                return walk(inner)
            case Store(inner, _, _):
                return walk(inner) or check_store(n)
        return None

    failure = walk(node)
    if failure is not None:
        return failure
    if len(invocations(node)) > max_invocations:
        return fail(
            None,
            None,
            error_result("E_TOO_MANY", f"too many commands (max {max_invocations})", max=max_invocations),
        )
    return outcome
