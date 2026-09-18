"""Parameter declarations for custom commands (spec §5.3, ADR-0009 action item 3).

A custom command's parameters are stored as JSON and turned into the same `Param` objects built-in
specs use, so validation, usage text and `!help` work identically for both.
"""

from __future__ import annotations

import shlex
from typing import Any

from doomtp_bot.runtime.spec import PARAM_TYPES, Param, validate_params

FIELDS = ("name", "type", "required", "default", "min", "max", "max_len", "choices", "description")
BOOLS = {"yes": True, "no": False, "true": True, "false": False, "on": True, "off": False}


class ParamError(ValueError):
    """The declaration was malformed: bad position, unknown type, unknown field."""


def to_params(declared: Any) -> tuple[Param, ...]:
    """Stored JSON → Param objects, ordered by position. Unusable rows are dropped rather than raising,
    so one bad row saved by an older version can't make a command unrunnable."""
    params: list[Param] = []
    for row in declared or ():
        if not isinstance(row, dict) or "position" not in row or "name" not in row:
            continue
        fields = {k: v for k, v in row.items() if k in (*FIELDS, "position")}
        if isinstance(fields.get("choices"), list):
            fields["choices"] = tuple(fields["choices"])
        try:
            params.append(Param(**fields))
        except TypeError:
            continue
    params.sort(key=lambda p: p.index)
    try:
        validate_params(tuple(params))
    except ValueError:
        return ()
    return tuple(params)


def _value(key: str, raw: str) -> Any:
    if key in ("min", "max"):
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError as exc:
            raise ParamError(f"{key} must be a number") from exc
    if key == "max_len":
        if not raw.isdigit():
            raise ParamError("max_len must be a whole number")
        return int(raw)
    if key == "required":
        if raw.lower() not in BOOLS:
            raise ParamError("required must be yes or no")
        return BOOLS[raw.lower()]
    if key == "choices":
        return tuple(c.strip() for c in raw.split(",") if c.strip())
    if key == "type" and raw not in PARAM_TYPES:
        raise ParamError(f"type must be one of: {', '.join(sorted(PARAM_TYPES))}")
    return raw


def declare(
    existing: Any, position: str, assignments: list[str], description: str = ""
) -> list[dict[str, Any]]:
    """Add or replace the declaration at `position`. `assignments` are `key=value` words (spec §5.3).

    Returns the new JSON-ready list. Raises ParamError with a user-facing message.
    """
    fields: dict[str, Any] = {"position": position}
    for word in assignments:
        key, sep, raw = word.partition("=")
        key = key.strip().lower()
        if not sep or key not in FIELDS:
            raise ParamError(f"expected key=value from: {', '.join(FIELDS)}")
        fields[key] = _value(key, raw.strip())
    if "name" not in fields:
        raise ParamError("a parameter needs name=<name>")
    if description:
        fields["description"] = description
    rows = [dict(r) for r in (existing or ()) if isinstance(r, dict) and r.get("position") != position]
    rows.append(fields)
    rows.sort(key=lambda r: str(r.get("position", "")))
    check = to_params(rows)
    if len(check) != len(rows):
        raise ParamError("positions must run 1, 2, 3… with at most one N+ capture, last")
    return rows


def remove(existing: Any, position: str) -> list[dict[str, Any]]:
    return [dict(r) for r in (existing or ()) if isinstance(r, dict) and r.get("position") != position]


def split_declaration(text: str) -> tuple[list[str], str]:
    """`pos name=x type=int "how many"` → (["name=x", "type=int"], "how many"). Quotes group the text."""
    try:
        words = shlex.split(text)
    except ValueError as exc:
        raise ParamError("unbalanced quotes in the declaration") from exc
    assignments = [w for w in words if "=" in w]
    description = " ".join(w for w in words if "=" not in w)
    return assignments, description


def describe(params: tuple[Param, ...]) -> str:
    """One line per parameter for `!cc info` and `!help`."""
    if not params:
        return "takes free arguments"
    return "; ".join(
        f"{p.position} {p.name}: {p.type}{'' if p.required else ' (optional)'}"
        + (f" — {p.description}" if p.description else "")
        for p in params
    )
