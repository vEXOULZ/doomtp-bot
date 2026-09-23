"""Text normalization for filter matching (architecture §9).

Matching happens on a normalized copy of the text, so `𝔫🅸ceee`, `n i c e` and `nïcé` all look like
`nice`. Every normalized character remembers which original character it came from, so a match can be
masked in the *original* text without disturbing anything else.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Characters that carry no visible content and are used to break up words.
INVISIBLE = frozenset("​‌‍⁠﻿­\U000e0000")
# Confusables and leetspeak, applied after NFKD and mark stripping.
CONFUSABLES = {
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "6": "g", "7": "t", "8": "b", "9": "g",
    "@": "a", "$": "s", "!": "i", "|": "i", "£": "l", "€": "e", "+": "t", "¡": "i",
    "ø": "o", "œ": "oe", "æ": "ae", "ß": "ss", "ð": "d", "þ": "th", "ł": "l", "đ": "d", "ı": "i",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Normalized:
    """Normalized text plus, for each character, the index it came from in the original."""

    text: str
    origin: tuple[int, ...]

    def span(self, start: int, end: int) -> tuple[int, int]:
        """Map a match on the normalized text back to a slice of the original."""
        if not self.origin or start >= len(self.origin):
            return (0, 0)
        first = self.origin[start]
        last = self.origin[min(end, len(self.origin)) - 1]
        return (first, last + 1)


def normalize(text: str) -> Normalized:
    """Fold case, accents, confusables and repeated letters, keeping a map back to the original text.

    Separators are kept: patterns tolerate them between letters instead (matcher.GAP), so `b a d` is
    caught without joining innocent word pairs.
    """
    chars: list[str] = []
    origin: list[int] = []
    for index, raw in enumerate(text):
        if raw in INVISIBLE:
            continue
        for decomposed in unicodedata.normalize("NFKD", raw):
            if unicodedata.combining(decomposed):  # accents, after NFKD
                continue
            folded = CONFUSABLES.get(decomposed.casefold(), decomposed.casefold())
            for char in folded:
                if chars and chars[-1] == char:
                    continue  # "niiiice" → "nice"; patterns are normalized the same way
                chars.append(char)
                origin.append(index)
    return Normalized("".join(chars), tuple(origin))


def normalize_pattern(pattern: str) -> str:
    """A literal pattern, normalized the same way as the text it will be matched against."""
    return normalize(pattern).text
