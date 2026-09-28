"""Variable keys, per-run write buffer and the store protocol (spec §6.5, ADR-0010).

The runtime only depends on `VariableStore` and `VariableAccess`. Part of the implementation (Postgres store,
access matrix) lives in doomtp_bot.variables.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from doomtp_bot.runtime.namespaces import VAR_NAMESPACES, is_reserved_var_name
from doomtp_bot.runtime.result import Code, CommandError, ErrorCode, Result, error_result, json_size
from doomtp_bot.runtime.values import MISSING

if TYPE_CHECKING:
    from doomtp_bot.runtime.context import ExecContext

# Storage limits (ADR-0019). The quota and the per-value cap are per owner and live in `variable_limits`;
# these are the defaults, and MAX_VALUE_BYTES is the ceiling no cap may exceed, checked when a write is
# buffered so no run builds a value bigger than any store would take.
DEFAULT_QUOTA_BYTES = 1024 * 1024
DEFAULT_VALUE_CAP_BYTES = 256 * 1024
MAX_VALUE_BYTES = 1024 * 1024
MAX_QUOTA_BYTES = 1024 * 1024 * 1024
OWNER_KINDS = ("channel", "publisher", "chatter")
MAX_LIST_ITEMS = 100
MAX_NAMES_PER_SPACE = 200
VAR_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class VariableError(CommandError):
    """An ErrorCode names itself in `data.error` (spec §6.2); a plain Code (126) carries no identifier."""

    def result(self) -> Result:
        if isinstance(self.code, ErrorCode):
            return error_result(self.code.name, self.message)
        return Result.failure(self.code, self.message)


@dataclass(frozen=True, slots=True)
class VarKey:
    ns: str
    key1: str
    key2: str = ""
    key3: str = ""
    name: str = ""

    def label(self) -> str:
        return f"{self.ns}.{self.name}"


_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(b|k|kb|m|mb|g|gb)?$", re.IGNORECASE)
_SIZE_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_size(text: str) -> int | None:
    """`4096`, `256KB`, `1.5mb` → bytes, counting in 1024s. None if it isn't a size."""
    match = _SIZE_RE.match(text.strip())
    if match is None:
        return None
    unit = (match.group(2) or "b")[0].lower()
    return int(float(match.group(1)) * _SIZE_UNITS[unit])


def format_size(size: int) -> str:
    for unit, factor in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= factor:
            return f"{size / factor:.1f}".removesuffix(".0") + " " + unit
    return f"{size} B"


def owner_of(key: VarKey | Space) -> tuple[str, str]:
    """The quota owner of a key: its namespace's first segment and key1 (`channel.chatter` → the channel)."""
    return key.ns.split(".", 1)[0], key.key1


@dataclass(frozen=True, slots=True)
class Limits:
    quota_bytes: int = DEFAULT_QUOTA_BYTES
    value_cap_bytes: int = DEFAULT_VALUE_CAP_BYTES


def check_value_cap(key: VarKey, size: int, limits: Limits) -> None:
    if size > limits.value_cap_bytes:
        raise VariableError(
            f"{key.label()} is too large ({size} bytes, max {limits.value_cap_bytes})",
            ErrorCode.E_VALUE_TOO_BIG,
        )


def check_quota(owner: tuple[str, str], used: int, grew: bool, limits: Limits) -> None:
    """A commit that leaves its owner over quota fails, unless it only shrank what was stored."""
    if grew and used > limits.quota_bytes:
        raise VariableError(
            f"{owner[0]} storage is full ({used} of {limits.quota_bytes} bytes)", ErrorCode.E_QUOTA
        )


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
        raise VariableError(f"unknown variable namespace {namespace}", ErrorCode.E_BAD_NAMESPACE)
    if not VAR_NAME_RE.match(name) or is_reserved_var_name(namespace, name):
        raise VariableError(f"invalid variable name {namespace}.{name}", ErrorCode.E_BAD_VAR_NAME)
    chatter = ctx.invoker.id if ctx.invoker else None
    channel = ctx.channel.id
    owner = ctx.publisher.id if ctx.publisher else None
    if "chatter" in namespace.split(".") and chatter is None:
        raise VariableError(f"{namespace}.{name} needs a chatter", ErrorCode.E_BAD_REFERENCE)
    if namespace.startswith("publisher") and owner is None:
        raise VariableError(
            f"{namespace}.{name} is only available inside custom commands", ErrorCode.E_BAD_REFERENCE
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
        raise VariableError(
            f"value too large ({size} bytes, max {MAX_VALUE_BYTES})", ErrorCode.E_VALUE_TOO_BIG
        )


# ── operations and buffer ───────────────────────────────────────────────────
OpKind = Literal["set", "append", "incr", "delete", "pop"]


@dataclass(frozen=True, slots=True)
class WriteOp:
    kind: OpKind
    key: VarKey
    value: Any = None  # set: value; append: item; incr: number; pop: index
    path: tuple[str | int, ...] = ()  # keys and indexes inside the value: channel.stats[kills] (ADR-0018)

    def label(self) -> str:
        return self.key.label() + "".join(f"[{p}]" for p in self.path)


def apply_op(current: Any, op: WriteOp) -> Any:
    """Pure application of one op to a current value (MISSING if absent). Raises VariableError.

    With a path, the op applies to what the path points at. Writes create missing maps on the way;
    `delete` and `pop` need everything they walk through to be there.
    """
    if not op.path:
        return _apply_leaf(current, op)
    creates = op.kind not in ("delete", "pop")
    if current is MISSING:
        if not creates:
            raise VariableError(f"{op.key.label()} doesn't exist", ErrorCode.E_KEY)
        root: Any = {}
    else:
        root = copy.deepcopy(current)
    _apply_at(root, op.path, op, creates)
    return root


def _apply_at(container: Any, path: tuple[str | int, ...], op: WriteOp, creates: bool) -> None:
    key, rest = path[0], path[1:]
    if isinstance(container, dict):
        name = str(key)
        if name not in container:
            if not creates:
                raise VariableError(f"{op.label()}: no key {name}", ErrorCode.E_KEY)
            if rest:
                container[name] = {}
        if rest:
            _apply_at(container[name], rest, op, creates)
        elif op.kind == "delete":
            del container[name]
        else:
            container[name] = _apply_leaf(container.get(name, MISSING), op)
    elif isinstance(container, list):
        position = _position(key, container, op)
        if rest:
            _apply_at(container[position], rest, op, creates)
        elif op.kind == "delete":
            del container[position]
        else:
            container[position] = _apply_leaf(container[position], op)
    else:
        raise VariableError(
            f"{op.label()}: can't go inside a value that isn't a map or list", ErrorCode.E_NOT_A_MAP
        )


def _position(key: str | int, items: list[Any], op: WriteOp) -> int:
    """A list index, negative from the end, down to -len (ADR-0019 D4c)."""
    try:
        position = int(key)
    except ValueError:
        raise VariableError(
            f"{op.label()}: a list takes a number, not {key}", ErrorCode.E_NOT_A_MAP
        ) from None
    if not -len(items) <= position < len(items):
        raise VariableError(f"{op.label()}: no item {position} (it has {len(items)})", ErrorCode.E_INDEX)
    return position


def _apply_leaf(current: Any, op: WriteOp) -> Any:
    if op.kind == "set":
        return op.value
    if op.kind == "delete":
        return MISSING
    if op.kind == "append":
        if current is MISSING:
            return [op.value]
        if not isinstance(current, list):
            raise VariableError(f"{op.label()} is not a list", ErrorCode.E_NOT_A_LIST)
        if len(current) >= MAX_LIST_ITEMS:
            raise VariableError(f"{op.label()} is full (max {MAX_LIST_ITEMS} items)", ErrorCode.E_LIST_FULL)
        return [*current, op.value]
    if op.kind == "pop":
        if current is MISSING:
            raise VariableError(f"{op.label()} doesn't exist", ErrorCode.E_KEY)
        if not isinstance(current, list):
            raise VariableError(f"{op.label()} is not a list", ErrorCode.E_NOT_A_LIST)
        if not current:
            raise VariableError(f"{op.label()} is empty", ErrorCode.E_EMPTY)
        items = list(current)
        del items[_position(-1 if op.value is None else op.value, items, op)]
        return items
    # incr
    base = 0 if current is MISSING else current
    if isinstance(base, bool) or not isinstance(base, (int, float)):
        raise VariableError(f"{op.label()} is not a number", ErrorCode.E_NOT_A_NUMBER)
    return base + op.value


def pop_item(current: Any, op: WriteOp) -> Any:
    """The item `op` (a pop) would remove, read before it is buffered. Raises what the pop would."""
    items = _at(current, op)
    _apply_leaf(items, op)  # the same checks as the pop itself
    return items[_position(-1 if op.value is None else op.value, items, op)]


def _at(current: Any, op: WriteOp) -> Any:
    for key in op.path:
        if isinstance(current, dict):
            if str(key) not in current:
                raise VariableError(f"{op.label()}: no key {key}", ErrorCode.E_KEY)
            current = current[str(key)]
        elif isinstance(current, list):
            current = current[_position(key, current, op)]
        elif current is MISSING:
            raise VariableError(f"{op.key.label()} doesn't exist", ErrorCode.E_KEY)
        else:
            raise VariableError(
                f"{op.label()}: can't go inside a value that isn't a map or list", ErrorCode.E_NOT_A_MAP
            )
    return current


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
                        f"too many variables in {op.key.ns} (max {MAX_NAMES_PER_SPACE})",
                        ErrorCode.E_TOO_MANY_NAMES,
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


ANY = "*"  # declares every variable; only `!var`, the documented exception, may (architecture §4.2)


class DeclaredVariables:
    """A built-in's view of the run's variables: only what its spec declares in `reads` and `writes`.

    Declarations are `namespace.name`, e.g. `chatter.location`. Anything else fails the command with 126,
    so what the docs and `!explain` say a command touches is all it can touch (architecture §4.2).
    Expression stores (`> channel.x`) and placeholders are not a command's own reads and writes: the
    access policy governs those.
    """

    def __init__(
        self, session: VariableSession, command: str, reads: tuple[str, ...], writes: tuple[str, ...]
    ) -> None:
        self.session = session
        self.command = command
        self.reads = frozenset(reads) | frozenset(writes)  # a command reads what it is about to change
        self.writes = frozenset(writes)

    @property
    def access(self) -> VariableAccess:
        return self.session.access

    async def get(self, key: VarKey) -> Any:
        self._check(key, self.reads, "read")
        return await self.session.get(key)

    async def buffer(self, op: WriteOp) -> Any:
        self._check(op.key, self.writes, "write")
        return await self.session.buffer(op)

    def _check(self, key: VarKey, declared: frozenset[str], verb: str) -> None:
        if ANY not in declared and key.label() not in declared:
            raise VariableError(
                f"{self.command} may not {verb} {key.label()}: its spec doesn't declare it", Code.DENIED
            )


class InMemoryVariableStore:
    """Process-local store for tests and for running without a database. Every owner gets `limits`."""

    def __init__(self, limits: Limits | None = None) -> None:
        self.data: dict[VarKey, Any] = {}
        self.limits = limits or Limits()

    async def get(self, key: VarKey) -> Any:
        return self.data.get(key, MISSING)

    async def names_in(self, space: Space) -> set[str]:
        return {k.name for k in self.data if Space(k.ns, k.key1, k.key2, k.key3) == space}

    async def commit(self, ops: Iterable[WriteOp], ctx: ExecContext) -> None:
        staged = dict(self.data)
        grown: dict[tuple[str, str], int] = {}
        for op in ops:
            current = staged.get(op.key, MISSING)
            value = apply_op(current, op)
            before = 0 if current is MISSING else json_size(current)
            after = 0 if value is MISSING else json_size(value)
            if value is MISSING:
                staged.pop(op.key, None)
            else:
                check_value_cap(op.key, after, self.limits)
                staged[op.key] = value
            owner = owner_of(op.key)
            grown[owner] = grown.get(owner, 0) + after - before
        for owner, delta in grown.items():
            used = sum(json_size(v) for k, v in staged.items() if owner_of(k) == owner)
            check_quota(owner, used, delta > 0, self.limits)
        self.data = staged
