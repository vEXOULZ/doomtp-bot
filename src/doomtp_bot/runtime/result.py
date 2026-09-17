"""Result model and exit codes (command-language-spec §6.1–§6.2)."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from typing import TypeAlias

Value: TypeAlias = None | bool | int | float | str | list["Value"] | dict[str, "Value"]

MAX_DATA_BYTES = 4096
MAX_MESSAGE_CHARS = 2000


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


# Codes whose final Result is never sent to chat (spec §6.6).
SILENT_CODES = frozenset({Code.DENIED, Code.COOLDOWN, Code.CANCELLED})


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
        return len(json.dumps(self.data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    @classmethod
    def success(cls, message: str | None = None, data: Value = None) -> Result:
        return cls(Code.OK, message, data)

    @classmethod
    def failure(cls, code: int, message: str | None = None, data: Value = None) -> Result:
        if code == Code.OK:
            raise ValueError("failure() requires a non-zero code")
        return cls(code, message, data)
