"""What the bot sends for a final Result (spec §6.6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from doomtp_bot.lang.parser import Context
from doomtp_bot.runtime.result import Code, Result

Origin = Literal["parse", "preflight", "runtime"]
CallbackKind = Literal["on_cooldown", "on_denied"]


@dataclass(frozen=True, slots=True)
class OutputDecision:
    send: str | None
    callback: CallbackKind | None = None


def decide_output(
    result: Result,
    *,
    origin: Origin,
    context: Context,
    failed_index: int | None = None,
    quiet_errors: bool = False,
    parse_error_visible: bool = False,
) -> OutputDecision:
    code = result.code
    message = result.message or None
    if code == Code.DENIED:
        return OutputDecision(None, "on_denied")
    if code == Code.COOLDOWN:
        return OutputDecision(None, "on_cooldown")
    if code == Code.CANCELLED:
        return OutputDecision(None)
    if origin == "parse":
        return OutputDecision(message if parse_error_visible and not quiet_errors else None)
    if code == Code.OK:
        return OutputDecision(message)
    if code == Code.UNKNOWN and failed_index == 1 and context is Context.LINE:
        return OutputDecision(None)
    return OutputDecision(None if quiet_errors else message)
