"""AST node types (command-language-spec §4) and a canonical text form used by the conformance corpus."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

type Span = tuple[int, int]


@dataclass(frozen=True, slots=True)
class Text:
    value: str


# ── Expressions (spec §2.7, ADR-0018) ───────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Lit:
    value: Any  # int | float | str | bool


@dataclass(frozen=True, slots=True)
class Ref:
    """A name the bot defines: `$chatter.name`, `arg.1`, `event.user`, `_`, `_2.code`.

    `root` is `$chatter`, `$channel`, `$publisher`, `$bot`, `$now`, `arg`, `args`, `event`, `match`,
    `cooldown`, `denied`, `run`, `cmd`, `_` or `_N`. Dots walk only names the bot defines; brackets
    (`Index`) walk into values.
    """

    root: str
    path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Index:
    target: Expr
    key: Expr


@dataclass(frozen=True, slots=True)
class Access:
    """`:len`, `:keys`, `:values`, or a cast such as `:int` or `:choice(a,b)`."""

    target: Expr
    name: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Unary:
    op: str  # "-" | "not"
    operand: Expr


@dataclass(frozen=True, slots=True)
class Binary:
    op: str  # + - * / // % and or ??
    left: Expr
    right: Expr


@dataclass(frozen=True, slots=True)
class Compare:
    """`a < b <= c`, chained as in Python: each operand is evaluated once."""

    first: Expr
    rest: tuple[tuple[str, Expr], ...]


@dataclass(frozen=True, slots=True)
class Subst:
    """`{!cmd args}`: one invocation whose data (or message) is the value."""

    inv: Invocation


@dataclass(frozen=True, slots=True)
class Placeholder:
    expr: Expr
    fallback: tuple[Text | Placeholder, ...] | None
    span: Span


@dataclass(frozen=True, slots=True)
class VarRef:
    """A variable: `channel.deaths`, or a store target with a path, `channel.stats[kills]`."""

    namespace: str  # chatter | channel | channel.chatter | publisher | ...
    name: str
    path: tuple[Expr, ...] = ()


type Expr = Lit | Ref | VarRef | Index | Access | Unary | Binary | Compare | Subst | Placeholder
type Part = Text | Placeholder
type Arg = tuple[Part, ...]


@dataclass(frozen=True, slots=True)
class ArgSource:
    """An argument as it was typed, for `{arg.N+raw}` (spec §7.3): the whitespace before it and its
    source text, quotes and escapes intact. Its placeholders stay unexpanded, as the same `Placeholder`
    nodes the argument holds, so the runtime splices in the values it already computed for them."""

    gap: str
    parts: tuple[str | Placeholder, ...]


@dataclass(frozen=True, slots=True)
class Invocation:
    index: int  # 1-based pre-order index within its scope (spec §6.4); negative inside `{!…}`
    name: str
    personal: bool
    args: tuple[Arg, ...]
    raw_tail: str | None
    span: Span
    expr: Expr | None = None  # `check` and `calc`: the rest of the stage, parsed as one expression
    # One per argument, for `+raw`. Left out of equality: it restates the arguments, spacing aside.
    sources: tuple[ArgSource, ...] = field(default=(), compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class And:
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Or:
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Pipe:
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Group:
    inner: Node


@dataclass(frozen=True, slots=True)
class Store:
    inner: Node
    target: VarRef
    append: bool


@dataclass(frozen=True, slots=True)
class IfElse:
    """`ifelse cond ( then ) [ ( else ) ]`: both branches are checked, only the chosen one runs."""

    cond: Arg
    then: Group
    else_: Group | None


type Node = And | Or | Pipe | Group | Store | Invocation | IfElse


# ── Walking ─────────────────────────────────────────────────────────────────
def sub_exprs(expr: Expr) -> Iterator[Expr]:
    """The direct children of an expression (a placeholder's fallback placeholders included)."""
    match expr:
        case Index(target, key):
            yield target
            yield key
        case Access(target, _, _):
            yield target
        case Unary(_, operand):
            yield operand
        case Binary(_, left, right):
            yield left
            yield right
        case Compare(first, rest):
            yield first
            for _, e in rest:
                yield e
        case VarRef(path=path):
            yield from path
        case Placeholder(inner, fallback, _):
            yield inner
            for part in fallback or ():
                if isinstance(part, Placeholder):
                    yield part


def walk_expr(expr: Expr) -> Iterator[Expr]:
    """Every expression inside `expr`, pre-order, not descending into `{!…}` invocations."""
    yield expr
    for child in sub_exprs(expr):
        yield from walk_expr(child)


def arg_exprs(invocation: Invocation) -> Iterator[Expr]:
    """The top-level expressions of an invocation: its placeholders and its `check`/`calc` expression."""
    for arg in invocation.args:
        for part in arg:
            if isinstance(part, Placeholder):
                yield part
    if invocation.expr is not None:
        yield invocation.expr


def substitutions(exprs: Iterator[Expr]) -> list[Invocation]:
    """The `{!…}` invocations directly inside these expressions (not the ones nested in their args)."""
    return [e.inv for top in exprs for e in walk_expr(top) if isinstance(e, Subst)]


def arg_substitutions(arg: Arg) -> list[Invocation]:
    return substitutions(p for p in arg if isinstance(p, Placeholder))


def _with_substitutions(inv: Invocation) -> list[Invocation]:
    out = [inv]
    for inner in substitutions(arg_exprs(inv)):
        out += _with_substitutions(inner)
    return out


def invocations(node: Node) -> list[Invocation]:
    """All invocations in pre-order source order; `{!…}` ones right after the invocation holding them."""
    match node:
        case Invocation():
            return _with_substitutions(node)
        case And(left, right) | Or(left, right) | Pipe(left, right):
            return invocations(left) + invocations(right)
        case Group(inner) | Store(inner, _, _):
            return invocations(inner)
        case IfElse(cond, then, else_):
            found = [i for inv in arg_substitutions(cond) for i in _with_substitutions(inv)]
            return found + invocations(then) + (invocations(else_) if else_ is not None else [])
    raise TypeError(f"not an AST node: {node!r}")


def stores(node: Node) -> list[Store]:
    """Every variable write in the expression, in source order."""
    match node:
        case Invocation():
            return []
        case And(left, right) | Or(left, right) | Pipe(left, right):
            return stores(left) + stores(right)
        case Group(inner):
            return stores(inner)
        case Store(inner, _, _):
            return [*stores(inner), node]
        case IfElse(_, then, else_):
            return stores(then) + (stores(else_) if else_ is not None else [])
    raise TypeError(f"not an AST node: {node!r}")


# ── Canonical text form ─────────────────────────────────────────────────────
# Used by tests/lang/corpus.yaml. Examples:
#   Pipe(random["1-100"], echo["dice","rolled","a","{_1}!"])
#   Or(And(a[], Pipe(b[], Store(c[], channel.x))), d[])
#   cc["add","roll"]~"!random 1-6 | echo {_1}"
#   calc{1 + (2 * 3)}
# Literal "{" / "}" inside Text are rendered escaped (\{ \}) so they differ from placeholders.
# Inside an expression, every operation below the top one is parenthesised, so precedence is visible.


_BARE_KEY = re.compile(r"(?!_\d*$)[A-Za-z_][A-Za-z0-9_]*")


def _render_lit(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def _render_key(key: Expr) -> str:
    """A bracket key as written: `[kills]` for a word key, else the expression (`["best run"]`, `[arg.1]`)."""
    if isinstance(key, Lit) and isinstance(key.value, str) and _BARE_KEY.fullmatch(key.value):
        return f"[{key.value}]"
    return f"[{render_expr(key)}]"


def render_expr(expr: Expr, top: bool = True) -> str:
    def inner(e: Expr) -> str:
        return render_expr(e, top=False)

    def wrap(text: str) -> str:
        return text if top else f"({text})"

    match expr:
        case Lit(value):
            return _render_lit(value)
        case Ref(root, path):
            return ".".join((root, *path))
        case VarRef(namespace, name, path):
            return f"{namespace}.{name}" + "".join(_render_key(k) for k in path)
        case Index(target, key):
            return inner(target) + _render_key(key)
        case Access(target, name, choices):
            return f"{inner(target)}:{name}" + (f"({','.join(choices)})" if name == "choice" else "")
        case Unary(op, operand):
            return wrap(f"-{inner(operand)}" if op == "-" else f"not {inner(operand)}")
        case Binary(op, left, right):
            return wrap(f"{inner(left)} {op} {inner(right)}")
        case Compare(first, rest):
            return wrap(inner(first) + "".join(f" {op} {inner(e)}" for op, e in rest))
        case Subst(inv):
            return "{!" + to_canonical(inv) + "}"
        case Placeholder():
            return render_placeholder(expr)
    raise TypeError(f"not an expression: {expr!r}")


def render_placeholder(ph: Placeholder) -> str:
    if isinstance(ph.expr, Subst) and ph.fallback is None:
        return render_expr(ph.expr)  # `{!cmd}` is its own placeholder
    text = render_expr(ph.expr)
    if ph.fallback is not None:
        text += " ?? " + render_parts(ph.fallback)
    return "{" + text + "}"


def render_parts(parts: tuple[Part, ...]) -> str:
    out: list[str] = []
    for part in parts:
        if isinstance(part, Text):
            out.append(part.value.replace("{", "\\{").replace("}", "\\}"))
        else:
            out.append(render_placeholder(part))
    return "".join(out)


def to_canonical(node: Node) -> str:
    match node:
        case Invocation(name=name, personal=personal, args=args, raw_tail=raw_tail, expr=expr):
            head = ("@" if personal else "") + name
            rendered = "[" + ",".join(json.dumps(render_parts(a), ensure_ascii=False) for a in args) + "]"
            tail = "~" + json.dumps(raw_tail, ensure_ascii=False) if raw_tail is not None else ""
            if expr is not None:
                return head + "{" + render_expr(expr) + "}"
            return head + rendered + tail
        case And(left, right):
            return f"And({to_canonical(left)}, {to_canonical(right)})"
        case Or(left, right):
            return f"Or({to_canonical(left)}, {to_canonical(right)})"
        case Pipe(left, right):
            return f"Pipe({to_canonical(left)}, {to_canonical(right)})"
        case Group(inner):
            return f"Group({to_canonical(inner)})"
        case Store(inner, target, append):
            op = "Append" if append else "Store"
            return f"{op}({to_canonical(inner)}, {render_expr(target)})"
        case IfElse(cond, then, else_):
            branches = to_canonical(then) + (f", {to_canonical(else_)}" if else_ is not None else "")
            return f"IfElse({json.dumps(render_parts(cond), ensure_ascii=False)}, {branches})"
    raise TypeError(f"not an AST node: {node!r}")
