"""Parse error codes and hints (command-language-spec §3.4, Appendix C.8)."""

from __future__ import annotations

import enum


class ParseErrorCode(enum.StrEnum):
    UNTERMINATED_QUOTE = "E_UNTERMINATED_QUOTE"
    BAD_PLACEHOLDER = "E_BAD_PLACEHOLDER"
    RESERVED_OPERATOR = "E_RESERVED_OPERATOR"
    UNEXPECTED_OPERATOR = "E_UNEXPECTED_OPERATOR"
    MISSING_OPERAND = "E_MISSING_OPERAND"
    UNBALANCED_GROUP = "E_UNBALANCED_GROUP"
    BAD_NAME = "E_BAD_NAME"
    DYNAMIC_NAME = "E_DYNAMIC_NAME"
    DYNAMIC_VARREF = "E_DYNAMIC_VARREF"
    BAD_VARREF = "E_BAD_VARREF"
    RAW_TAIL_POSITION = "E_RAW_TAIL_POSITION"
    TOO_LONG = "E_TOO_LONG"
    INTERNAL = "E_INTERNAL"


HINTS: dict[ParseErrorCode, str] = {
    ParseErrorCode.UNTERMINATED_QUOTE: 'missing closing " (use \\" for a literal quote)',
    ParseErrorCode.BAD_PLACEHOLDER: "invalid placeholder (use \\{ for a literal brace)",
    ParseErrorCode.RESERVED_OPERATOR: "; is reserved (use && or ||)",
    ParseErrorCode.UNEXPECTED_OPERATOR: "unexpected {op} (quote it for literal text)",
    ParseErrorCode.MISSING_OPERAND: "expected a command after {op}",
    ParseErrorCode.UNBALANCED_GROUP: "unbalanced parentheses",
    ParseErrorCode.BAD_NAME: "invalid command name",
    ParseErrorCode.DYNAMIC_NAME: "command names can't be placeholders",
    ParseErrorCode.DYNAMIC_VARREF: "store targets can't be placeholders",
    ParseErrorCode.BAD_VARREF: "invalid variable (e.g. channel.deaths)",
    ParseErrorCode.RAW_TAIL_POSITION: "{name} must be used alone",
    ParseErrorCode.TOO_LONG: "expression too long",
    ParseErrorCode.INTERNAL: "internal parser error",
}


class ParseError(Exception):
    """A thrown parse failure. `offset` is a 0-based character offset; `column` is 1-based (spec §C.8)."""

    def __init__(self, code: ParseErrorCode, offset: int, **fmt: str) -> None:
        self.code = code
        self.offset = offset
        self.hint = HINTS[code].format(**fmt) if fmt else HINTS[code]
        super().__init__(f"parse error: {code.value} at {self.column}: {self.hint}")

    @property
    def column(self) -> int:
        return self.offset + 1
