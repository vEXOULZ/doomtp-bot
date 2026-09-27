"""The language's grammar as the site shows it: the railroad EBNF and one entry per rule (ADR-0011).

The diagrams themselves are drawn ahead of time by scripts/render_railroad.py into `static/grammar/`;
`GET /api/v1/grammar` hands out this text and these rules to caption and describe them.
"""

from __future__ import annotations

from pathlib import Path

# The wheel carries a copy beside the static files (see pyproject), because `docs/` isn't installed;
# in a source checkout the repository's own copy is the one being edited, so it wins.
_GRAMMAR_FILES = (
    Path(__file__).resolve().parents[3] / "docs" / "grammar" / "railroad.ebnf",
    Path(__file__).parent / "static" / "railroad.ebnf",
)
_GRAMMAR_FILE = next((f for f in _GRAMMAR_FILES if f.is_file()), _GRAMMAR_FILES[0])
# Read once at import: the same grammar CI checks against the spec.
GRAMMAR = _GRAMMAR_FILE.read_text(encoding="utf-8") if _GRAMMAR_FILE.is_file() else ""


def _grammar_rules(text: str) -> list[dict[str, str]]:
    """`Name ::= body` per rule, to caption and describe the diagrams drawn by scripts/render_railroad.py.
    An indented line continues the rule above it, the way the file is written."""
    found: list[dict[str, str]] = []
    for line in text.splitlines():
        name, sep, body = line.partition("::=")
        if sep and name.strip() and not line[0].isspace():
            found.append({"name": name.strip(), "body": " ".join(body.split())})
        elif found and line.strip():
            found[-1]["body"] += " " + " ".join(line.split())
    return found


GRAMMAR_RULES = _grammar_rules(GRAMMAR)
