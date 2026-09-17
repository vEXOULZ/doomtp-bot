"""Variable keys, per-run write buffer and the store protocol (spec §6.5, ADR-0010).

The runtime only depends on `VariableStore` and `VariableAccess`. Part of the implementation (SQLite store,
access matrix) lives in doomtp_bot.variables.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from doomtp_bot.runtime.namespaces import VAR_NAMESPACES, is_reserved_var_name
from doomtp_bot.runtime.result import Code, CommandError, Result, error_result, json_size
from doomtp_bot.runtime.values import MISSING

if TYPE_CHECKING:
    from doomtp_bot.runtime.context import ExecContext

MAX_VALUE_BYTES = 2048
MAX_LIST_ITEMS = 100
MAX_NAMES_PER_SPACE = 200
VAR_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class VariableError(CommandError):
    def __init__(self, code: int, message: str, error: str | None = None) -> None:
        super().__init__(message, code)
        self.error = error

    def result(self) -> Result:
        if self.error is None:
            return Result.failure(self.code, self.message)
        return error_result(self.error, self.message, self.code)


@dataclass(frozen=True, slots=True)
class VarKey:
    ns: str
    key1: str
    key2: str = ""
    key3: str = ""
    name: str = ""

    def label(self) -> str:
        return f"{self.ns}.{self.name}"


@dataclass(frozen=True, slots=True)
class Space:
    """(ns, key1, key2, key3) without a name — used for per-space limits and listing."""

    ns: str
    key1: str
    key2: str = ""
    key3: str = ""


def key_for(ctx: ExecContext, namespace: str, name: str) -> VarKey:
    """Build the storage key for `namespace.name` in this run. Raises VariableError if unaddressable."""
    if namespace not in VAR_NAMESPACES:
        raise VariableError(Code.USAGE, f"unknown variable namespace {namespace}")
    if not VAR_NAME_RE.match(name) or is_reserved_var_name(namespace, name):
        raise VariableError(Code.USAGE, f"invalid variable name {namespace}.{name}")
    chatter = ctx.invoker.id if ctx.invoker else None
    channel = ctx.channel.id
    owner = ctx.publisher.id if ctx.publisher else None
    if "chatter" in namespace.split(".") and chatter is None:
        raise VariableError(Code.USAGE, f"{namespace}.{name} needs a chatter", "E_BAD_REFERENCE")
    if namespace.startswith("publisher") and owner is None:
        raise VariableError(
            Code.USAGE, f"{namespace}.{name} is only available inside custom commands", "E_BAD_REFERENCE"
        )
    match namespace:
        case "chatter":
            return VarKey(namespace, chatter or "", name=name)
        case "channel":
            return VarKey(namespace, channel, name=name)
        case "channel.chatter":
            return VarKey(namespace, channel, chatter or "", name=name)
        case "publisher":
            return VarKey(namespace, owner or "", name=name)
        case "publisher.chatter":
            return VarKey(namespace, owner or "", chatter or "", name=name)
        case "publisher.channel":
            return VarKey(namespace, owner or "", channel, name=name)
        case _:  # publisher.channel.chatter
            return VarKey(namespace, owner or "", channel, chatter or "", name=name)


def check_value_size(value: Any) -> None:
    size = json_size(value)
    if size > MAX_VALUE_BYTES:
        raise VariableError(Code.USAGE, f"value too large ({size} bytes, max {MAX_VALUE_BYTES})")


# ── operations and buffer ───────────────────────────────────────────────────
OpKind = Literal["set", "append", "incr", "delete"]


@dataclass(frozen=True, slots=True)
class WriteOp:
    kind: OpKind
    key: VarKey
    value: Any = None  # set: value; append: item; incr: number


def apply_op(current: Any, op: WriteOp) -> Any:
    """Pure application of one op to a current value (MISSING if absent). Raises VariableError."""
    if op.kind == "set":
        return op.value
    if op.kind == "delete":
        return MISSING
    if op.kind == "append":
        if current is MISSING:
            return [op.value]
        if not isinstance(current, list):
            raise VariableError(Code.USAGE, f"{op.key.label()} is not a list")
        items = [*current, op.value]
        return items[-MAX_LIST_ITEMS:]
    # incr
    base = 0 if current is MISSING else current
    if isinstance(base, bool) or not isinstance(base, (int, float)):
        raise VariableError(Code.USAGE, f"{op.key.label()} is not a number")
    return base + op.value


class VariableStore(Protocol):
    async def get(self, key: VarKey) -> Any:
        """Committed value, or MISSING."""

    async def names_in(self, space: Space) -> set[str]:
        """Names currently stored in a space (for the per-space limit)."""

    async def commit(self, ops: Iterable[WriteOp], ctx: ExecContext) -> None:
        """Apply ops atomically, in order."""


class VariableAccess(Protocol):
    def can_write(self, ctx: ExecContext, namespace: str, name: str) -> bool: ...


class AllowAllAccess:
    def can_write(self, ctx: ExecContext, namespace: str, name: str) -> bool:
        return True


@dataclass
class VariableSession:
    """Per-run view: committed store plus a write buffer with read-your-writes (§6.5)."""

    store: VariableStore
    access: VariableAccess = field(default_factory=AllowAllAccess)
    ops: list[WriteOp] = field(default_factory=list)
    _overlay: dict[VarKey, Any] = field(default_factory=dict)

    async def get(self, key: VarKey) -> Any:
        if key in self._overlay:
            return copy.deepcopy(self._overlay[key])
        return copy.deepcopy(await self.store.get(key))

    async def buffer(self, op: WriteOp) -> Any:
        current = await self.get(op.key)
        new_value = apply_op(current, op)
        if new_value is not MISSING:
            check_value_size(new_value)
            if current is MISSING:
                space = Space(op.key.ns, op.key.key1, op.key.key2, op.key.key3)
                names = await self.store.names_in(space) | {
                    k.name
                    for k, v in self._overlay.items()
                    if v is not MISSING and Space(k.ns, k.key1, k.key2, k.key3) == space
                }
                if op.key.name not in names and len(names) >= MAX_NAMES_PER_SPACE:
                    raise VariableError(
                        Code.USAGE, f"too many variables in {op.key.ns} (max {MAX_NAMES_PER_SPACE})"
                    )
        self._overlay[op.key] = new_value
        self.ops.append(op)
        return new_value

    def discard(self) -> None:
        self.ops.clear()
        self._overlay.clear()

    async def commit(self, ctx: ExecContext) -> list[WriteOp]:
        ops = list(self.ops)
        if ops:
            await self.store.commit(ops, ctx)
        self.discard()
        return ops


class InMemoryVariableStore:
    """Process-local store for tests and for running without a database."""

    def __init__(self) -> None:
        self.data: dict[VarKey, Any] = {}

    async def get(self, key: VarKey) -> Any:
        return self.data.get(key, MISSING)

    async def names_in(self, space: Space) -> set[str]:
        return {k.name for k in self.data if Space(k.ns, k.key1, k.key2, k.key3) == space}

    async def commit(self, ops: Iterable[WriteOp], ctx: ExecContext) -> None:
        staged = dict(self.data)
        for op in ops:
            value = apply_op(staged.get(op.key, MISSING), op)
            if value is MISSING:
                staged.pop(op.key, None)
            else:
                staged[op.key] = value
        self.data = staged
