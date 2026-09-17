"""Runs tests/lang/corpus.yaml against the parser (spec Appendix A / C, ADR-0011)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from doomtp_bot.lang.ast import to_canonical
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import Context, NotACommand, ParserParams, RawTail, parse, preprocess_line

CORPUS = yaml.safe_load((Path(__file__).parent / "corpus.yaml").read_text(encoding="utf-8"))
RESERVED_VAR_NAMES = {"chatter", "channel", "data", "code", "message", "public", "root"}


def _raw_tail_fn(table: dict[str, dict[str, int]]):  # type: ignore[no-untyped-def]
    def raw_tail_from(name: str, lead_args: Sequence[str]) -> int | RawTail:
        entry = table.get(name)
        if entry is None:
            return RawTail.NONE
        if "" in entry:
            return entry[""]
        if not lead_args:
            return RawTail.MORE
        return entry.get(lead_args[0], RawTail.NONE)

    return raw_tail_from


def _params(case: dict[str, Any]) -> ParserParams:
    table = case.get("raw_tail", CORPUS["defaults"]["raw_tail"])
    return ParserParams(
        prefix=case.get("prefix", "!"),
        raw_tail_from=_raw_tail_fn(table),
        reserved_var_names=lambda ns, name: name in RESERVED_VAR_NAMES,
    )


@pytest.mark.parametrize("case", CORPUS["cases"], ids=[c["id"] for c in CORPUS["cases"]])
def test_corpus_case(case: dict[str, Any]) -> None:
    context = Context(case.get("context", "line"))
    text = case["input"]
    if context is Context.LINE:
        text = preprocess_line(text, case.get("reply_parent_login"))

    if case.get("not_command"):
        with pytest.raises(NotACommand):
            parse(text, context, _params(case))
        return

    if "error" in case:
        with pytest.raises(ParseError) as info:
            parse(text, context, _params(case))
        assert info.value.code.value == case["error"]
        if "column" in case:
            assert info.value.column == case["column"]
        return

    assert to_canonical(parse(text, context, _params(case))) == case["ast"]


def test_corpus_ids_unique() -> None:
    ids = [c["id"] for c in CORPUS["cases"]]
    assert len(ids) == len(set(ids))
