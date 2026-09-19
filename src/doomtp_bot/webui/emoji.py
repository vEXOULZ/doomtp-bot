"""Twemoji images for the emoji the site shows (architecture §11).

The bot's command sign is an emoji, and every platform draws it differently — on some Windows fonts
🏜 is a flat monochrome glyph, which is a poor thing to hand somebody as "the character you type".
So the pages swap the emoji we ship an image for, and leave everything else to the reader's fonts.

Two rules make this safe to apply everywhere:
  * only characters with a file in `static/emoji/` are replaced, so nothing silently disappears;
  * the image keeps the character as its `alt`, so copying the sign out of the page still copies text.

The art is Twemoji (CC-BY 4.0) — see `static/emoji/ATTRIBUTION.md`. Files are named after their
codepoints, the way Twemoji names them, so dropping another `.svg` in that folder is all it takes.
"""

from __future__ import annotations

import re
from pathlib import Path

from markupsafe import Markup, escape

STATIC_DIR = Path(__file__).parent / "static"
EMOJI_DIR = STATIC_DIR / "emoji"
VARIATION_SELECTOR = "️"  # some clients send 🏜️, some send 🏜; both mean the same sign


def _available() -> dict[str, str]:
    """Emoji we have art for, as character → URL, read once at import."""
    found: dict[str, str] = {}
    for file in sorted(EMOJI_DIR.glob("*.svg")):
        try:
            character = "".join(chr(int(part, 16)) for part in file.stem.split("-"))
        except ValueError:  # not a codepoint name: not ours to use
            continue
        found[character] = f"/static/emoji/{file.name}"
    return found


AVAILABLE = _available()
_PATTERN = (
    re.compile(
        "|".join(
            re.escape(character) + re.escape(VARIATION_SELECTOR) + "?"
            for character in sorted(AVAILABLE, key=len, reverse=True)  # longest first: sequences win
        )
    )
    if AVAILABLE
    else None
)


def emojify(text: object) -> Markup:
    """HTML for a string, with the emoji we ship replaced by their images."""
    escaped = escape(text)
    if _PATTERN is None:
        return escaped

    def image(match: re.Match[str]) -> str:
        character = match.group().rstrip(VARIATION_SELECTOR)
        return (
            f'<img class="emoji" src="{AVAILABLE[character]}" alt="{character}"'
            f' draggable="false" loading="lazy">'
        )

    return Markup(_PATTERN.sub(image, str(escaped)))


def has_emoji(text: str) -> bool:
    return _PATTERN is not None and _PATTERN.search(text) is not None


def finalize(value: object) -> object:
    """Jinja's per-expression hook: only strings carrying a known emoji are rewritten."""
    if hasattr(value, "__html__"):  # already markup — a macro's output, or something marked safe
        return value
    if isinstance(value, str) and has_emoji(value):
        return emojify(value)
    return value
