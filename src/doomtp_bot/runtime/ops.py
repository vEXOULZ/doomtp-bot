"""Expression operators as pure functions (spec §2.7, ADR-0018).

The evaluator and the operator commands (`add`, `eq`, …) share these, so `{1 + 2}` and `add 1 2` can't
disagree. Every failure is an `ExprError` carrying its ErrorCode; the executor turns it into a Result.
"""

from __future__ import annotations

import math
import re
from typing import Any

from doomtp_bot.runtime.result import ErrorCode, Result, error_result
from doomtp_bot.runtime.values import MISSING, render

MAX_INT = 10**18 - 1  # results past 18 digits overflow (ADR-0018 D8j)

_INT_TEXT = re.compile(r"^[+-]?\d{1,18}$")
_FLOAT_TEXT = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


class ExprError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def result(self) -> Result:
        return error_result(self.code.name, self.message)


def as_number(value: Any) -> int | float | None:
    """An int or float as is, numeric text parsed; anything else (bools included) is not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _INT_TEXT.match(text):
            return int(text)
        if _FLOAT_TEXT.match(text):
            number = float(text)
            return number if math.isfinite(number) else None
    return None


def literal(text: str) -> Any:
    """A command argument read as a value: a number, `true`/`false`, or else the text itself."""
    number = as_number(text)
    if number is not None:
        return number
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    return text


def truthy(value: Any) -> bool:
    """Python's rules: false, 0, 0.0, "", [] and {} are false; everything else is true."""
    return bool(value) and value is not MISSING


def holds(value: Any) -> bool:
    """A condition (`check`, `ifelse`). Text is read like an argument first, so `false` or `0` from chat,
    a stored text value or a `??` fallback is false, the same as typing it: `check false`."""
    return truthy(literal(value) if isinstance(value, str) else value)


def _describe(value: Any) -> str:
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a map"
    if isinstance(value, (int, float)):
        return "a number"
    return f'"{render(value)}"' if isinstance(value, str) and len(value) <= 20 else "text"


def number(value: Any, op: str) -> int | float:
    found = as_number(value)
    if found is None:
        raise ExprError(ErrorCode.E_TYPE, f"{op} needs numbers, not {_describe(value)}")
    return found


def checked(value: int | float) -> int | float:
    if isinstance(value, int):
        if abs(value) > MAX_INT:
            raise ExprError(ErrorCode.E_OVERFLOW, "number too large (more than 18 digits)")
        return value
    if not math.isfinite(value):
        raise ExprError(ErrorCode.E_OVERFLOW, "number too large")
    return value


def apply_binary(op: str, left: Any, right: Any) -> Any:
    """`+ - * / // %`. `and`, `or` and `??` short-circuit, so the evaluator handles them."""
    if op == "+" and isinstance(left, list) and isinstance(right, list):
        return [*left, *right]
    a, b = number(left, op), number(right, op)
    match op:
        case "+":
            return checked(a + b)
        case "-":
            return checked(a - b)
        case "*":
            return checked(a * b)
    if b == 0:
        raise ExprError(ErrorCode.E_DIV_ZERO, "division by zero")
    match op:
        case "/":
            return checked(a / b)
        case "//":
            return checked(a // b)
        case "%":
            return checked(a % b)
    raise ExprError(ErrorCode.E_UNKNOWN_OP, f"unknown operator {op}")


def apply_unary(op: str, operand: Any) -> Any:
    if op == "not":
        return not truthy(operand)
    return checked(-number(operand, "-"))


def _equal(left: Any, right: Any) -> bool:
    a, b = as_number(left), as_number(right)
    if a is not None and b is not None:
        return a == b
    if isinstance(left, (list, dict)) or isinstance(right, (list, dict)):
        return bool(left == right)
    return render(left) == render(right)


def contains(item: Any, container: Any) -> bool:
    """`in`: a list's items, a map's keys, or a piece of text."""
    if isinstance(container, list):
        return any(_equal(item, x) for x in container)
    if isinstance(container, dict):
        return render(item) in container
    if isinstance(container, str):
        return render(item) in container
    raise ExprError(ErrorCode.E_TYPE, f"in needs a list, a map or text, not {_describe(container)}")


def compare(op: str, left: Any, right: Any) -> bool:
    """Numeric when both sides are numbers, else text; case-sensitive (ADR-0018 D8h)."""
    match op:
        case "==":
            return _equal(left, right)
        case "!=":
            return not _equal(left, right)
        case "in":
            return contains(left, right)
        case "not in":
            return not contains(left, right)
    a, b = as_number(left), as_number(right)
    if a is None or b is None:
        if isinstance(left, (list, dict)) or isinstance(right, (list, dict)):
            raise ExprError(ErrorCode.E_TYPE, f"{op} can't order {_describe(left)} and {_describe(right)}")
        a, b = render(left), render(right)  # type: ignore[assignment]
    match op:
        case "<":
            return a < b  # type: ignore[operator]
        case "<=":
            return a <= b  # type: ignore[operator]
        case ">":
            return a > b  # type: ignore[operator]
        case ">=":
            return a >= b  # type: ignore[operator]
    raise ExprError(ErrorCode.E_UNKNOWN_OP, f"unknown operator {op}")


def index(target: Any, key: Any) -> Any:
    """`x[key]`: a map by key, a list by position (negative from the end). Not there → MISSING."""
    if target is MISSING or target is None:
        return MISSING
    if isinstance(target, dict):
        return target.get(render(key), MISSING)
    if isinstance(target, list):
        position = as_number(key)
        if not isinstance(position, int):
            raise ExprError(ErrorCode.E_TYPE, f"a list takes a whole-number index, not {_describe(key)}")
        if -len(target) <= position < len(target):
            return target[position]
        return MISSING
    raise ExprError(ErrorCode.E_TYPE, f"can't index {_describe(target)} with [ ]")


def length(value: Any) -> int:
    if isinstance(value, (list, dict, str)):
        return len(value)
    raise ExprError(ErrorCode.E_TYPE, f":len needs a list, a map or text, not {_describe(value)}")


def keys(value: Any) -> list[str]:
    if not isinstance(value, dict):
        raise ExprError(ErrorCode.E_NOT_A_MAP, f":keys needs a map, not {_describe(value)}")
    return list(value)


def values(value: Any) -> list[Any]:
    if not isinstance(value, dict):
        raise ExprError(ErrorCode.E_NOT_A_MAP, f":values needs a map, not {_describe(value)}")
    return list(value.values())


BINARY_COMMANDS = {"add": "+", "sub": "-", "mul": "*", "div": "/", "idiv": "//", "mod": "%"}
COMPARE_COMMANDS = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">=", "in": "in"}
