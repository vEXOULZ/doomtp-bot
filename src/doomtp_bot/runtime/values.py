"""Value lookup, type conversion and rendering (command-language-spec §7.3–§7.6)."""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final
from urllib.parse import urlsplit

from doomtp_bot.runtime.result import Result, Value


class _Missing:
    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _Missing()

UserResolver = Callable[[str], Awaitable[dict[str, Any] | None]]


def is_missing(value: object) -> bool:
    """§7.3.3: missing, null, or the empty string all trigger `??` fallbacks."""
    return value is MISSING or value is None or value == ""


def descend(value: Any, path: Sequence[str]) -> Any:
    """Walk a data path; any mismatch yields MISSING (§7.3.1)."""
    for segment in path:
        if isinstance(value, dict):
            if segment not in value:
                return MISSING
            value = value[segment]
        elif isinstance(value, list):
            if not segment.isdigit() or int(segment) >= len(value):
                return MISSING
            value = value[int(segment)]
        else:
            return MISSING
    return value


def result_value(result: Result, path: Sequence[str]) -> Any:
    """Lookup inside a Result: bare → scalar data or message; code/message/data select parts; else into data."""
    if not path:
        if result.data is not None and not isinstance(result.data, (list, dict)):
            return result.data
        return MISSING if result.message is None else result.message
    head, *rest = path
    if head == "code":
        return result.code if not rest else MISSING
    if head == "message":
        return (MISSING if result.message is None else result.message) if not rest else MISSING
    if head == "data":
        return descend(result.data, rest)
    return descend(result.data, path)


# ── rendering (§7.6) ────────────────────────────────────────────────────────
def render_float(x: float) -> str:
    if math.isnan(x) or math.isinf(x):
        return str(x)
    if x.is_integer() and abs(x) < 1e15:
        return str(int(x))
    if 1e-6 <= abs(x) < 1e15:
        text = repr(x)
        if "e" in text or "E" in text or len(text.split(".", 1)[-1]) > 6:
            text = f"{x:.6f}".rstrip("0").rstrip(".")
        return text
    return repr(x)


def render(value: Value | Any) -> str:
    if value is None or value is MISSING:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return render_float(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(render(v) for v in value)
    if isinstance(value, dict):
        import json

        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


# ── type conversion (§7.4) ──────────────────────────────────────────────────
class ConversionError(ValueError):
    pass


_INT_RE = re.compile(r"^[+-]?\d{1,18}$")
_RANGE_RE = re.compile(r"^(-?\d+)-(-?\d+)$")
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_USER_RE = re.compile(r"^@?([A-Za-z0-9_]{1,25})$")
_BOOL_TRUE = frozenset({"true", "yes", "on", "1"})
_BOOL_FALSE = frozenset({"false", "no", "off", "0"})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else render(value).strip()


async def convert(
    value: Any,
    type_name: str,
    *,
    choices: Sequence[str] = (),
    resolve_user: UserResolver | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    max_len: int | None = None,
) -> Any:
    """Convert `value` per §7.4; raises ConversionError with a user-facing reason."""
    text = _text(value)
    converted: Any
    if type_name == "str":
        converted = value if isinstance(value, str) else render(value)
        if max_len is not None and len(converted) > max_len:
            raise ConversionError(f"at most {max_len} characters")
        return converted
    if type_name == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            converted = value
        elif _INT_RE.match(text):
            converted = int(text)
        else:
            raise ConversionError("expected a whole number")
    elif type_name == "float":
        try:
            converted = (
                float(text)
                if not isinstance(value, (int, float)) or isinstance(value, bool)
                else float(value)
            )
        except ValueError as exc:
            raise ConversionError("expected a number") from exc
        if math.isnan(converted) or math.isinf(converted):
            raise ConversionError("expected a finite number")
    elif type_name == "bool":
        if isinstance(value, bool):
            return value
        lowered = text.lower()
        if lowered in _BOOL_TRUE:
            return True
        if lowered in _BOOL_FALSE:
            return False
        raise ConversionError("expected yes/no")
    elif type_name == "range":
        match = _RANGE_RE.match(text)
        if not match or int(match.group(1)) > int(match.group(2)):
            raise ConversionError("expected a range like 1-100")
        return {"lo": int(match.group(1)), "hi": int(match.group(2))}
    elif type_name == "duration":
        if _INT_RE.match(text):
            converted = int(text)
        else:
            match = _DURATION_RE.match(text)
            if not text or not match or not any(match.groups()):
                raise ConversionError("expected a duration like 10m or 1h30m")
            h, m, s = (int(g) if g else 0 for g in match.groups())
            converted = h * 3600 + m * 60 + s
        if converted < 0:
            raise ConversionError("duration must not be negative")
    elif type_name == "user":
        match = _USER_RE.match(text)
        if not match:
            raise ConversionError("expected a Twitch user name")
        if resolve_user is None:
            raise ConversionError("user lookup unavailable")
        user = await resolve_user(match.group(1).lower())
        if user is None:
            raise ConversionError(f"unknown user {match.group(1)}")
        return user
    elif type_name == "choice":
        for choice in choices:
            if text.lower() == choice.lower():
                return choice
        raise ConversionError("expected one of: " + ", ".join(choices))
    elif type_name == "url":
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConversionError("expected an http(s) URL")
        return text
    else:
        raise ConversionError(f"unknown type {type_name}")

    if minimum is not None and converted < minimum:
        raise ConversionError(f"must be at least {render(minimum)}")
    if maximum is not None and converted > maximum:
        raise ConversionError(f"must be at most {render(maximum)}")
    return converted
