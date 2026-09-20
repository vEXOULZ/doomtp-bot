"""AST node types (command-language-spec §4) and a canonical text form used by the conformance corpus."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeAlias

Span: TypeAlias = tuple[int, int]


@dataclass(frozen=True, slots=True)
class Text:
    value: str


@dataclass(frozen=True, slots=True)
class TypeSpec:
    name: str  # str|int|float|bool|range|duration|user|url|choice
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Placeholder:
    root: str
    path: tuple[str, ...]
    type: TypeSpec | None
    fallback: tuple[Text | Placeholder, ...] | None
    span: Span


Part: TypeAlias = Text | Placeholder
Arg: TypeAlias = tuple[Part, ...]


@dataclass(frozen=True, slots=True)
class VarRef:
    namespace: str  # chatter | channel | channel.chatter | publisher | ...
    name: str


@dataclass(frozen=True, slots=True)
class Invocation:
    index: int  # 1-based pre-order index within its scope (spec §6.4)
    name: str
    personal: bool
    args: tuple[Arg, ...]
    raw_tail: str | None
    span: Span


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


Node: TypeAlias = And | Or | Pipe | Group | Store | Invocation


def invocations(node: Node) -> list[Invocation]:
    """All invocations in pre-order source order."""
    match node:
        case Invocation():
            return [node]
        case And(left, right) | Or(left, right) | Pipe(left, right):
            return invocations(left) + invocations(right)
        case Group(inner) | Store(inner, _, _):
            return invocations(inner)
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
    raise TypeError(f"not an AST node: {node!r}")


# ── Canonical text form ─────────────────────────────────────────────────────
# Used by tests/lang/corpus.yaml. Examples:
#   Pipe(random["1-100"], echo["dice","rolled","a","{1}!"])
#   Or(And(a[], Pipe(b[], Store(c[], channel.x))), d[])
#   cc["add","roll"]~"!random 1-6 | echo {1}"
# Literal "{" / "}" inside Text are rendered escaped (\{ \}) so they differ from placeholders.


def render_placeholder(ph: Placeholder) -> str:
    ref = ".".join((ph.root, *ph.path))
    if ph.type is not None:
        ref += ":" + (f"choice({','.join(ph.type.choices)})" if ph.type.name == "choice" else ph.type.name)
    if ph.fallback is not None:
        ref += " ?? " + render_parts(ph.fallback)
    return "{" + ref + "}"


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
        case Invocation(name=name, personal=personal, args=args, raw_tail=raw_tail):
            head = ("@" if personal else "") + name
            rendered = "[" + ",".join(json.dumps(render_parts(a), ensure_ascii=False) for a in args) + "]"
            tail = "~" + json.dumps(raw_tail, ensure_ascii=False) if raw_tail is not None else ""
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
            return f"{op}({to_canonical(inner)}, {target.namespace}.{target.name})"
    raise TypeError(f"not an AST node: {node!r}")
