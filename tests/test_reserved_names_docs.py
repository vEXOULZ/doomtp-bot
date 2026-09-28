"""The reserved variable names written in ADR-0010 and the access matrix match `runtime/namespaces.py` (ADR-0019 item 11)."""

import re
from pathlib import Path

import pytest

from doomtp_bot.runtime.namespaces import _RESERVED_BY_NAMESPACE, RESERVED_EVERYWHERE, VAR_NAMESPACES

DOCS = Path(__file__).resolve().parents[1] / "docs"
ROW = re.compile(r"^\s*\| (every namespace|`[a-z.]+\.`) \| (`[a-z_]+`(?:, `[a-z_]+`)*) \|$")


def documented(path: Path) -> dict[str, frozenset[str]]:
    table: dict[str, frozenset[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if m := ROW.match(line):
            namespace = "*" if m[1] == "every namespace" else m[1].strip("`").removesuffix(".")
            table[namespace] = frozenset(re.findall(r"`([a-z_]+)`", m[2]))
    return table


@pytest.mark.parametrize("doc", ["adr/0010-variables-scopes.md", "variable-access-matrix.md"])
def test_the_documented_reserved_names_are_the_code_s(doc: str) -> None:
    assert documented(DOCS / doc) == {"*": RESERVED_EVERYWHERE, **_RESERVED_BY_NAMESPACE}


def test_the_reserved_namespaces_are_real_ones() -> None:
    assert set(_RESERVED_BY_NAMESPACE) <= set(VAR_NAMESPACES)
