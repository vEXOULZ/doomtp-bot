"""Command specifications: the single source for usage text, !help, /api/v1/commands (ADR-0005, spec §5.3)."""

from __future__ import annotations

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from doomtp_bot.runtime.result import Result  # noqa: F401 - used in string annotations


class InputMode(enum.StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class LogLevel(enum.StrEnum):
    OFF = "off"
    ERRORS = "errors"
    OUTPUT = "output"
    INVOCATIONS = "invocations"
    ALL = "all"


PARAM_TYPES = frozenset({"str", "int", "float", "bool", "range", "duration", "user", "url", "choice"})
_POSITION_RE = re.compile(r"^([1-9][0-9]*)(\+)?$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Cooldown:
    tier_s: int = 0
    user_s: int = 0


@dataclass(frozen=True, slots=True)
class Param:
    position: str  # "1", "2", "3+"
    name: str
    type: str = "str"
    required: bool = False
    default: Any = None
    description: str = ""
    min: float | None = None
    max: float | None = None
    max_len: int | None = None
    choices: tuple[str, ...] = ()

    @property
    def index(self) -> int:
        m = _POSITION_RE.match(self.position)
        assert m is not None
        return int(m.group(1))

    @property
    def variadic(self) -> bool:
        return self.position.endswith("+")


@dataclass(frozen=True, slots=True)
class Example:
    invocation: str
    output: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    module: str
    summary: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    params: tuple[Param, ...] = ()
    input: InputMode = InputMode.NONE
    data_schema: dict[str, str] = field(default_factory=dict)
    examples: tuple[Example, ...] = ()
    required_role: str = "everyone"
    default_cooldowns: dict[str, Cooldown] = field(default_factory=dict)
    log_level: LogLevel = LogLevel.INVOCATIONS
    side_effects: bool = False
    reads: tuple[str, ...] = ()  # declared variable reads, e.g. "chatter.location"
    writes: tuple[str, ...] = ()  # declared variable writes
    requires: tuple[str, ...] = ()  # channel capabilities (ADR-0007)
    toggleable: bool = True  # False: can never be disabled (core, core_admin)
    fixed_policy: bool = False  # sentinels (spec §8): role everyone, no cooldowns, not configurable
    raw_tail_from: int | None = None  # spec §3.3

    def __post_init__(self) -> None:
        validate_params(self.params)

    def usage(self) -> str:
        parts = [self.name]
        for p in self.params:
            label = p.name + ("…" if p.variadic else "")
            parts.append(f"<{label}>" if p.required else f"[{label}]")
        return " ".join(parts)


def validate_params(params: Sequence[Param]) -> None:
    """Enforce spec §5.3: contiguous positions, one trailing variadic, no required after optional."""
    seen_optional = False
    names: set[str] = set()
    for expected, p in enumerate(params, start=1):
        if not _POSITION_RE.match(p.position) or p.index != expected:
            raise ValueError(f"param positions must be contiguous from 1; got {p.position!r} at #{expected}")
        if p.variadic and expected != len(params):
            raise ValueError("only the last param may be variadic (N+)")
        if p.required and seen_optional:
            raise ValueError(f"required param {p.name!r} follows an optional one")
        seen_optional = seen_optional or not p.required
        if not _IDENT_RE.match(p.name) or p.name in names:
            raise ValueError(f"invalid or duplicate param name {p.name!r}")
        names.add(p.name)
        if p.type not in PARAM_TYPES:
            raise ValueError(f"unknown param type {p.type!r}")
        if p.type == "choice" and not p.choices:
            raise ValueError(f"choice param {p.name!r} needs choices")
