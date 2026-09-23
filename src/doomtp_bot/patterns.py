"""Patterns moderators write: listener regexes and filter entries (architecture §7, §9).

They run on every chat line and every message the bot sends, inside the event loop. Python's `re`
backtracks without limit and can't be interrupted, so a 7-character `(a+)+$`, or a filter wildcard
like `ab*ab*ab*ab*z`, meeting the wrong line would stall the bot in every channel, not just the one
whose moderator wrote it. The `regex` module takes the same syntax, avoids most of that backtracking,
and gives up after a timeout on the rest. A pattern that runs out of time counts as no match.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias, TypeVar

import regex
import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

    Pattern: TypeAlias = regex.Pattern[str]
    Match: TypeAlias = regex.Match[str]

log = structlog.get_logger(__name__)

# Generous for a 500-character chat line, and short enough that a hostile pattern costs little.
MATCH_TIMEOUT_S = 0.05
PatternError = regex.error
T = TypeVar("T")


def compile_pattern(pattern: str) -> Pattern:
    """Compile case-insensitively. Raises PatternError for an invalid pattern."""
    return regex.compile(pattern, regex.IGNORECASE)


def search(pattern: Pattern, text: str) -> Match | None:
    return _bounded(pattern, lambda: pattern.search(text, timeout=MATCH_TIMEOUT_S), None)


def find_all(pattern: Pattern, text: str) -> list[Match]:
    """Every match, left to right. The timeout covers the whole scan, not each match."""
    return _bounded(pattern, lambda: list(pattern.finditer(text, timeout=MATCH_TIMEOUT_S)), [])


def _bounded(pattern: Pattern, match: Callable[[], T], gave_up: T) -> T:
    try:
        return match()
    except TimeoutError:
        log.warning("pattern.timed_out", pattern=pattern.pattern, timeout_s=MATCH_TIMEOUT_S)
        return gave_up
