"""Railroad diagrams for the language page, drawn from `docs/grammar/railroad.ebnf` (ADR-0011 item 6).

One SVG per rule, written into the package's static files and committed — the runtime never generates
them, and the image has no need for this script. `--check` redraws into memory and fails if anything on
disk differs, which is what CI runs so a grammar edit can't leave stale pictures behind.

The EBNF here is the documentation grammar (spec Appendix D), which is small and fixed: terminals in
quotes, character classes in brackets, `( )` groups, `?`, `*`, `+`, and `|` alternatives.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

from railroad import Choice, Diagram, NonTerminal, OneOrMore, Optional, Sequence, Terminal, ZeroOrMore

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "grammar" / "railroad.ebnf"
OUT = ROOT / "src" / "doomtp_bot" / "webui" / "static" / "grammar"

# The site's own colours, spelled out: an <img> can't reach the page's CSS variables, but it does follow
# the reader's light or dark preference, which is what the page follows too.
CSS = """
svg.railroad-diagram { background: transparent; }
svg.railroad-diagram path { stroke-width: 2; stroke: #6d6459; fill: none; }
svg.railroad-diagram text { font: 13px ui-monospace, Consolas, monospace; text-anchor: middle;
  fill: #22201d; }
svg.railroad-diagram text.comment { font: italic 11px ui-monospace, Consolas, monospace; }
svg.railroad-diagram rect { stroke-width: 2; stroke: #e2dbd1; fill: #fff; }
svg.railroad-diagram rect.group-box { stroke: #a4621b; stroke-dasharray: 8 4; fill: none; }
svg.railroad-diagram .non-terminal rect { stroke: #a4621b; }
@media (prefers-color-scheme: dark) {
  svg.railroad-diagram path { stroke: #a99d8f; }
  svg.railroad-diagram text { fill: #efe7dd; }
  svg.railroad-diagram rect { stroke: #322c26; fill: #1b1815; }
  svg.railroad-diagram rect.group-box, svg.railroad-diagram .non-terminal rect { stroke: #e0a458; }
}
"""
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_TOKEN = re.compile(r"'[^']*'|\[[^\]]*\]|[A-Za-z][A-Za-z0-9_]*|::=|[()|?*+]|\S")


class GrammarError(ValueError):
    """The EBNF says something this small reader doesn't understand."""


def rules(text: str) -> list[tuple[str, str]]:
    """`Name ::= body` pairs, in file order. An indented line continues the rule above it."""
    found: list[tuple[str, str]] = []
    for line in _COMMENT.sub(" ", text).splitlines():
        if not line.strip():
            continue
        name, sep, body = line.partition("::=")
        if sep and name.strip() and not line[0].isspace():
            found.append((name.strip(), body.strip()))
        elif found:
            found[-1] = (found[-1][0], f"{found[-1][1]} {line.strip()}")
        else:
            raise GrammarError(f"a rule has to come before {line.strip()!r}")
    return found


class _Reader:
    """Alternatives of sequences of repeated atoms — the whole of this dialect."""

    def __init__(self, body: str) -> None:
        self.tokens = _TOKEN.findall(body)
        self.at = 0

    def peek(self) -> str | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> str:
        token = self.tokens[self.at]
        self.at += 1
        return token

    def alternatives(self) -> object:
        options = [self.sequence()]
        while self.peek() == "|":
            self.take()
            options.append(self.sequence())
        return options[0] if len(options) == 1 else Choice(0, *options)

    def sequence(self) -> object:
        items: list[object] = []
        while (token := self.peek()) is not None and token not in ("|", ")"):
            items.append(self.repeated())
        if not items:
            raise GrammarError("empty sequence")
        return items[0] if len(items) == 1 else Sequence(*items)

    def repeated(self) -> object:
        item = self.atom()
        while (token := self.peek()) in ("?", "*", "+"):
            self.take()
            item = {"?": Optional, "*": ZeroOrMore, "+": OneOrMore}[str(token)](item)
        return item

    def atom(self) -> object:
        token = self.take()
        if token == "(":
            inside = self.alternatives()
            if self.peek() != ")":
                raise GrammarError("unclosed (")
            self.take()
            return inside
        if token.startswith("'") and token.endswith("'"):
            return Terminal(token[1:-1])
        if token.startswith("["):
            return Terminal(token)  # a character class reads as the set it is
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", token):
            return NonTerminal(token)
        raise GrammarError(f"unexpected {token!r}")


def draw(name: str, body: str) -> str:
    reader = _Reader(body)
    diagram = Diagram(reader.alternatives(), css=None)
    if reader.peek() is not None:
        raise GrammarError(f"{name}: leftover {reader.peek()!r}")
    out = io.StringIO()
    diagram.writeStandalone(out.write, css=CSS)
    return out.getvalue().strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed SVGs are stale")
    args = parser.parse_args()

    drawn = {name: draw(name, body) for name, body in rules(SOURCE.read_text(encoding="utf-8"))}
    OUT.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, svg in drawn.items():
        file = OUT / f"{name}.svg"
        if args.check:
            if not file.is_file() or file.read_text(encoding="utf-8") != svg:
                stale.append(file.name)
        else:
            file.write_text(svg, encoding="utf-8", newline="\n")
    for file in sorted(OUT.glob("*.svg")):  # a rule that was renamed or removed leaves a picture behind
        if file.stem not in drawn:
            stale.append(file.name) if args.check else file.unlink()
    if stale:
        print(f"stale diagrams: {', '.join(sorted(stale))} — run scripts/render_railroad.py", file=sys.stderr)
        return 1
    print(f"{len(drawn)} diagrams {'checked' if args.check else 'written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
