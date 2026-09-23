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


def error_result(error: str, message: str, code: int = Code.USAGE, **fields: Value) -> Result:
    """A failure carrying the spec's error identifier in `data.error`, e.g. E_BAD_REFERENCE (spec §5.2)."""
    data: dict[str, Value] = {"error": error, **fields}
    return Result(code, message, data)


@dataclass(frozen=True, slots=True)
class Result:
    code: int = Code.OK
    message: str | None = None
    data: Value = None

    def __post_init__(self) -> None:
        if not 0 <= self.code <= 255:
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

    def __init__(self, message: str, code: int = Code.USAGE) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def result(self) -> Result:
        return Result.failure(self.code, self.message)
