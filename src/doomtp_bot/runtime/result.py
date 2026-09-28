"""Result model and exit codes (command-language-spec §6.1–§6.2)."""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# JSON-like data. Covariant containers so list[str] / dict[str, bool] are accepted; runtime values are list/dict.
type Value = None | bool | int | float | str | Sequence[Value] | Mapping[str, Value]

MAX_DATA_BYTES = 4096
MAX_MESSAGE_CHARS = 2000


def to_json(value: object) -> str:
    """Compact JSON used for every stored or size-checked value."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def json_size(value: object) -> int:
    return len(to_json(value).encode("utf-8"))


class Code(enum.IntEnum):
    OK = 0
    FAIL = 1
    USAGE = 2
    NOT_FOUND = 3
    TIMEOUT = 124
    UPSTREAM_LIMITED = 125
    DENIED = 126
    UNKNOWN = 127
    COOLDOWN = 128
    CANCELLED = 130


MAX_CODE = 1023  # 1–99 belong to commands, 100–1023 to the runtime (spec §6.2, ADR-0018)


class ErrorCode(enum.IntEnum):
    """Every runtime error identifier and its own exit code (spec §6.2). Blocks leave gaps for later ones."""

    # 200–219: parse errors (spec §3.4)
    E_UNTERMINATED_QUOTE = 200
    E_BAD_PLACEHOLDER = 201
    E_RESERVED_OPERATOR = 202
    E_UNEXPECTED_OPERATOR = 203
    E_MISSING_OPERAND = 204
    E_UNBALANCED_GROUP = 205
    E_BAD_NAME = 206
    E_DYNAMIC_NAME = 207
    E_DYNAMIC_VARREF = 208
    E_BAD_VARREF = 209
    E_RAW_TAIL_POSITION = 210
    E_TOO_LONG = 211
    E_EXPR_SYNTAX = 212
    E_UNKNOWN_OP = 213
    E_EXPR_TOO_DEEP = 214
    # 220–229: preflight (spec §5.2)
    E_TOO_MANY = 220
    E_INPUT_NOT_ACCEPTED = 221
    E_BAD_REFERENCE = 222
    E_CC_CYCLE = 223
    E_CC_DEPTH = 224
    # 230–249: evaluation
    E_MISSING_VALUE = 230
    E_DATA_TOO_LARGE = 231
    E_TYPE = 232
    E_DIV_ZERO = 233
    E_OVERFLOW = 234
    E_EXPR_BUDGET = 235
    E_SUBST_DEPTH = 236
    # 250–269: values and collections
    E_INDEX = 250
    E_KEY = 251
    E_NOT_A_LIST = 252
    E_NOT_A_MAP = 253
    E_EMPTY = 254
    E_NOT_A_NUMBER = 255
    # 300–399: storage
    E_LIST_FULL = 300
    E_QUOTA = 301
    E_VALUE_TOO_BIG = 302
    E_BAD_NAMESPACE = 303
    E_BAD_VAR_NAME = 304
    E_TOO_MANY_NAMES = 305
    # 299: a bug in the parser itself, never the user's fault
    E_INTERNAL = 299


ERROR_CODES = frozenset(int(error) for error in ErrorCode)


def error_result(error: str, message: str, code: int | None = None, **fields: Value) -> Result:
    """A failure carrying the spec's error identifier in `data.error`, e.g. E_BAD_REFERENCE (spec §5.2).

    The code is the identifier's own number from ErrorCode unless one is given.
    """
    data: dict[str, Value] = {"error": error, **fields}
    return Result(ErrorCode[error] if code is None else code, message, data)


@dataclass(frozen=True, slots=True)
class Result:
    code: int = Code.OK
    message: str | None = None
    data: Value = None

    def __post_init__(self) -> None:
        if not 0 <= self.code <= MAX_CODE:
            raise ValueError(f"exit code out of range: {self.code}")
        if self.message is not None and len(self.message) > MAX_MESSAGE_CHARS:
            object.__setattr__(self, "message", self.message[: MAX_MESSAGE_CHARS - 1] + "…")

    @property
    def ok(self) -> bool:
        return self.code == Code.OK

    def data_size(self) -> int:
        return json_size(self.data)

    @classmethod
    def success(cls, message: str | None = None, data: Value = None) -> Result:
        return cls(Code.OK, message, data)

    @classmethod
    def failure(cls, code: int, message: str | None = None, data: Value = None) -> Result:
        if code == Code.OK:
            raise ValueError("failure() requires a non-zero code")
        return cls(code, message, data)


class CommandError(Exception):
    """Raised by a handler (or runtime helper) to end the command with a failure Result."""

    def __init__(self, message: str, code: int = Code.USAGE, data: Value = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def result(self) -> Result:
        return Result.failure(self.code, self.message, self.data)
