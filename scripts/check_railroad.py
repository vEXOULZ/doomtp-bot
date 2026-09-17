"""CI check: docs/grammar/railroad.ebnf must equal the ```ebnf block in spec Appendix D (ADR-0011)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "command-language-spec.md"
COPY = ROOT / "docs" / "grammar" / "railroad.ebnf"


def main() -> int:
    spec = SPEC.read_text(encoding="utf-8")
    appendix = spec.split("## Appendix D.", 1)
    if len(appendix) != 2:
        print("Appendix D not found in spec", file=sys.stderr)
        return 1
    match = re.search(r"```ebnf\n(.*?)```", appendix[1], re.DOTALL)
    if not match:
        print("No ```ebnf block in Appendix D", file=sys.stderr)
        return 1
    if match.group(1).strip() != COPY.read_text(encoding="utf-8").strip():
        print(f"{COPY.relative_to(ROOT)} differs from spec Appendix D; copy the block over.", file=sys.stderr)
        return 1
    print("railroad.ebnf matches spec Appendix D")
    return 0


if __name__ == "__main__":
    sys.exit(main())
