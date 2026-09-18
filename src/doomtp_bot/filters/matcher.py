"""Compiling filter lists and applying them to text (architecture §9).

Word and wildcard entries match the *normalized* text, so `b4d`, `b a d` and `baaad` all hit `bad`.
Regex entries match the text as typed, because normalization folds digits and punctuation that a regex
usually cares about. Either way, hits are converted to positions in the original text before censoring,
so `mask` blanks exactly the offending characters. Allow entries cover the Scunthorpe problem: a hit
inside an allowed word is not a hit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from doomtp_bot.filters.normalize import Normalized, normalize, normalize_pattern

Kind = Literal["word", "wildcard", "regex", "allow"]
Action = Literal["mask", "replace", "tag", "block"]
Target = Literal["normalized", "raw"]
MASK_CHAR = "*"
MAX_PATTERN_CHARS = 200
# Characters people put between letters to slip a word past a filter: "b.a.d", "b a d", "b-a-d".
GAP = r"[\s._\-*'\"`~^,;:!|/\\]*"


class FilterError(ValueError):
    """A pattern that can't be compiled, or is too broad to be useful."""


@dataclass(frozen=True, slots=True)
class FilterEntry:
    id: int
    channel_id: str
    pattern: str
    kind: Kind = "word"
    action: Action = "mask"
    category: str = ""
    replacement: str = ""
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Hit:
    """A match, in positions of the original text."""

    entry_id: int
    pattern: str
    action: Action
    start: int
    end: int


@dataclass
class FilterResult:
    """`text` is None when an entry said `block`."""

    text: str | None
    hits: list[Hit] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.text is None

    @property
    def changed(self) -> bool:
        return bool(self.hits)

    def patterns(self) -> list[str]:
        seen: list[str] = []
        for hit in self.hits:
            if hit.pattern not in seen:
                seen.append(hit.pattern)
        return seen


def compile_entry(entry: FilterEntry) -> tuple[re.Pattern[str], Target]:
    """One entry → a regex plus which text it runs against. Raises FilterError for anything unusable."""
    if not entry.pattern or len(entry.pattern) > MAX_PATTERN_CHARS:
        raise FilterError(f"patterns must be 1–{MAX_PATTERN_CHARS} characters")
    if entry.kind == "regex":
        try:
            return re.compile(entry.pattern, re.IGNORECASE), "raw"
        except re.error as exc:
            raise FilterError(f"invalid regex: {exc}") from exc
    normalized = normalize_pattern(entry.pattern)
    if not normalized:
        raise FilterError("the pattern normalizes to nothing")
    if entry.kind == "wildcard":
        body = GAP.join(".*" if ch == "*" else re.escape(ch) for ch in normalized)
        return re.compile(rf"\b{body}", re.IGNORECASE), "normalized"
    body = GAP.join(re.escape(ch) for ch in normalized)  # word: tolerate separators between letters
    return re.compile(rf"\b{body}\b", re.IGNORECASE), "normalized"


class ChannelFilter:
    """The compiled entries for one scope, ready to apply."""

    def __init__(self, entries: list[FilterEntry]) -> None:
        self.blocking: list[tuple[FilterEntry, re.Pattern[str], Target]] = []
        self.allowed: list[tuple[re.Pattern[str], Target]] = []
        for entry in entries:
            if not entry.enabled:
                continue
            try:
                compiled, target = compile_entry(entry)
            except FilterError:
                continue  # a bad row saved earlier must not break every message
            if entry.kind == "allow":
                self.allowed.append((compiled, target))
            else:
                self.blocking.append((entry, compiled, target))

    def __bool__(self) -> bool:
        return bool(self.blocking)

    def apply(self, text: str) -> FilterResult:
        """Censor `text`. Returns the text to send (None when blocked) and what matched."""
        if not self.blocking or not text:
            return FilterResult(text)
        normalized = normalize(text)
        skip = [
            _span(match, normalized, target)
            for pattern, target in self.allowed
            for match in pattern.finditer(normalized.text if target == "normalized" else text)
        ]
        hits: list[Hit] = []
        for entry, pattern, target in self.blocking:
            for match in pattern.finditer(normalized.text if target == "normalized" else text):
                start, end = _span(match, normalized, target)
                if end <= start:
                    continue
                if any(low <= start and end <= high for low, high in skip):
                    continue  # inside an allowed word (the Scunthorpe problem)
                hits.append(Hit(entry.id, entry.pattern, entry.action, start, end))
        if not hits:
            return FilterResult(text)
        if any(hit.action == "block" for hit in hits):
            return FilterResult(None, hits)
        return FilterResult(self._censor(text, hits), hits)

    def _censor(self, text: str, hits: list[Hit]) -> str:
        entries = {entry.id: entry for entry, _, _ in self.blocking}
        out, at = [], 0
        for hit in sorted(hits, key=lambda h: h.start):
            if hit.start < at:  # overlapping hits: the first already covered this stretch
                continue
            out.append(text[at : hit.start])
            out.append(_censored(text[hit.start : hit.end], entries[hit.entry_id]))
            at = hit.end
        out.append(text[at:])
        return "".join(out)


def _span(match: re.Match[str], normalized: Normalized, target: Target) -> tuple[int, int]:
    if target == "raw":
        return match.span()
    return normalized.span(*match.span())


def _censored(matched: str, entry: FilterEntry) -> str:
    if entry.action == "replace":
        return entry.replacement or "…"
    if entry.action == "tag":
        return f"[{entry.category or 'filtered'}]"
    return MASK_CHAR * len(matched)  # mask
