"""Parser behaviour beyond the conformance corpus: indexes, spans, limits, pre-processing, edge cases."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from doomtp_bot.lang.ast import Invocation, Pipe, Placeholder, Text, invocations, to_canonical
from doomtp_bot.lang.errors import ParseError, ParseErrorCode
from doomtp_bot.lang.parser import (
    MAX_EXPR_CHARS,
    Context,
    NotACommand,
    ParserParams,
    RawTail,
    parse,
    preprocess_line,
)


def raw_tail_from(name: str, lead: Sequence[str]) -> int | RawTail:
    if name == "explain":
        return 1
    if name == "cc":
        if not lead:
            return RawTail.MORE
        return 3 if lead[0] in ("add", "edit") else RawTail.NONE
    return RawTail.NONE


PARAMS = ParserParams(
    raw_tail_from=raw_tail_from, reserved_var_names=lambda ns, n: n in {"chatter", "channel"}
)


def line(text: str, **kw: object) -> str:
    params = ParserParams(**{**PARAMS.__dict__, **kw}) if kw else PARAMS  # type: ignore[arg-type]
    return to_canonical(parse(preprocess_line(text), Context.LINE, params))


def body(text: str) -> str:
    return to_canonical(parse(text, Context.BODY, PARAMS))


def error(text: str, context: Context = Context.LINE) -> ParseError:
    with pytest.raises(ParseError) as info:
        parse(preprocess_line(text) if context is Context.LINE else text, context, PARAMS)
    return info.value


# ── indexes and spans ──────────────────────────────────────────────────────
def test_invocation_indexes_are_preorder_source_order() -> None:
    node = parse("( !a || b ) && c | d > channel.x", Context.LINE, PARAMS)
    assert [(i.index, i.name) for i in invocations(node)] == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]


def test_spans_cover_source() -> None:
    text = "!random 1-100 | echo {1}!"
    node = parse(text, Context.LINE, PARAMS)
    assert isinstance(node, Pipe)
    first, second = invocations(node)
    assert text[first.span[0] : first.span[1]] == "!random 1-100"
    assert text[second.span[0] : second.span[1]] == "echo {1}!"
    ph = second.args[0][0]
    assert isinstance(ph, Placeholder) and text[ph.span[0] : ph.span[1]] == "{1}"


def test_names_are_case_folded() -> None:
    assert line("!RaNdOm 1-6") == 'random["1-6"]'


# ── pre-processing ─────────────────────────────────────────────────────────
def test_preprocess_strips_invisible_padding_and_whitespace() -> None:
    assert preprocess_line("\u200b  !ping \U000e0000") == "!ping"


def test_preprocess_keeps_inner_invisible_characters() -> None:
    assert preprocess_line("!echo a\u200bb") == "!echo a\u200bb"


def test_preprocess_reply_mention_is_case_insensitive_and_needs_whitespace() -> None:
    assert preprocess_line("@Alice  !ping", "alice") == "!ping"
    assert preprocess_line("@alicex !ping", "alice") == "@alicex !ping"
    assert preprocess_line("@alice !ping", None) == "@alice !ping"


@pytest.mark.parametrize("text", ["", "hello", "!", "! ping", "!{x}", "(!ping)", "!!ping"])
def test_not_a_command(text: str) -> None:
    with pytest.raises(NotACommand):
        parse(preprocess_line(text), Context.LINE, PARAMS)


def test_multi_character_prefix() -> None:
    assert line("~>ping", prefix="~>") == "ping[]"


# ── words, quotes, escapes ─────────────────────────────────────────────────
def test_empty_quoted_argument() -> None:
    node = parse('!echo ""', Context.LINE, PARAMS)
    assert isinstance(node, Invocation) and node.args == ((),)


def test_escape_at_end_is_literal_backslash() -> None:
    assert line("!echo a\\") == 'echo["a\\\\"]'


def test_escaped_quote_inside_quotes() -> None:
    assert line('!echo "say \\"hi\\""') == 'echo["say \\"hi\\""]'


def test_placeholders_inside_quotes_are_parsed() -> None:
    node = parse('!echo "it\'s {1.celsius}C now!"', Context.LINE, PARAMS)
    assert isinstance(node, Invocation)
    parts = node.args[0]
    assert parts[0] == Text("it's ") and isinstance(parts[1], Placeholder) and parts[2] == Text("C now!")
    assert parts[1].root == "1" and parts[1].path == ("celsius",)


def test_body_allows_surrounding_whitespace() -> None:
    assert body("  echo hi  ") == 'echo["hi"]'


def test_prefix_optional_after_operators_and_in_body() -> None:
    assert line("!a | !b && c") == "And(Pipe(a[], b[]), c[])"
    assert body("!a | b") == "Pipe(a[], b[])"


# ── placeholders ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!echo {arg.3+}", 'echo["{arg.3+}"]'),
        ("!echo {arg.3+raw}", 'echo["{arg.3+raw}"]'),
        ("!echo {1.items.0}", 'echo["{1.items.0}"]'),
        ("!echo { chatter.name }", 'echo["{chatter.name}"]'),
        ("!echo {_.code}", 'echo["{_.code}"]'),
        ("!echo {x ?? y}", None),
        ("!echo {arg.1:int??5}", 'echo["{arg.1:int ?? 5}"]'),
        ("!echo {arg.1 ?? }", 'echo["{arg.1 ?? }"]'),
        ("!echo {arg.1:choice( a , b )}", 'echo["{arg.1:choice(a,b)}"]'),
        ("!echo {chatter.location ?? Lisbon, PT}", 'echo["{chatter.location ?? Lisbon, PT}"]'),
    ],
)
def test_placeholder_forms(text: str, expected: str | None) -> None:
    if expected is None:
        assert error(text).code is ParseErrorCode.BAD_PLACEHOLDER
    else:
        assert line(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "!echo {1",
        "!echo {0}",
        "!echo {arg.}",
        "!echo {arg.1:integer}",
        "!echo {arg.1:choice()}",
        "!echo {_x}",
    ],
)
def test_bad_placeholders(text: str) -> None:
    assert error(text).code is ParseErrorCode.BAD_PLACEHOLDER


def test_placeholder_nesting_limit() -> None:
    ok = "!echo {chatter.a ?? {chatter.b ?? {chatter.c ?? {chatter.d ?? x}}}}"
    assert line(ok).startswith('echo["{chatter.a')
    too_deep = "!echo {chatter.a ?? {chatter.b ?? {chatter.c ?? {chatter.d ?? {chatter.e ?? x}}}}}"
    assert error(too_deep).code is ParseErrorCode.BAD_PLACEHOLDER


# ── store targets ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!a > chatter.x", "Store(a[], chatter.x)"),
        ("!a > publisher.chatterbox", "Store(a[], publisher.chatterbox)"),
        ("!a > publisher.channel.round", "Store(a[], publisher.channel.round)"),
        ("( !a | b ) > channel.x", "Store(Group(Pipe(a[], b[])), channel.x)"),
    ],
)
def test_store_targets(text: str, expected: str) -> None:
    assert line(text) == expected


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("!a > channel.Deaths", ParseErrorCode.BAD_VARREF),
        ("!a > channels.x", ParseErrorCode.BAD_VARREF),
        ("!a > channel.", ParseErrorCode.BAD_VARREF),
        ("!a > chatter.x" + "y" * 32, ParseErrorCode.BAD_VARREF),
        ("!a > channel.x > channel.y", ParseErrorCode.UNEXPECTED_OPERATOR),
        ("!a > | b", ParseErrorCode.UNEXPECTED_OPERATOR),
        ("!a >", ParseErrorCode.MISSING_OPERAND),
    ],
)
def test_store_target_errors(text: str, code: ParseErrorCode) -> None:
    assert error(text).code is code


# ── operators and structure errors ─────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "code", "column"),
    [
        ("!a ||", ParseErrorCode.MISSING_OPERAND, 6),
        ("!a && ; b", ParseErrorCode.RESERVED_OPERATOR, 7),
        ("( !a ; )", ParseErrorCode.RESERVED_OPERATOR, 6),
        ("!a )", ParseErrorCode.UNBALANCED_GROUP, 4),
        ("!a (", ParseErrorCode.UNEXPECTED_OPERATOR, 4),
        ("( !a ) ( !b )", ParseErrorCode.UNEXPECTED_OPERATOR, 8),
        ("!a | {x}", ParseErrorCode.DYNAMIC_NAME, 6),
        ("!a | b$c", ParseErrorCode.BAD_NAME, 6),
        ("!" + "a" * 33, ParseErrorCode.BAD_NAME, 2),
    ],
)
def test_structure_errors_and_columns(text: str, code: ParseErrorCode, column: int) -> None:
    err = error(text)
    assert (err.code, err.column) == (code, column)


def test_error_message_format() -> None:
    err = error("!a | && !b")
    assert str(err) == "parse error: E_UNEXPECTED_OPERATOR at 6: unexpected && (quote it for literal text)"


def test_too_long() -> None:
    with pytest.raises(ParseError) as info:
        parse("echo " + "x" * MAX_EXPR_CHARS, Context.BODY, PARAMS)
    assert info.value.code is ParseErrorCode.TOO_LONG


# ── raw tails ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!explain", "explain[]"),
        ("!cc add", 'cc["add"]'),
        ("!cc add roll", 'cc["add","roll"]'),
        ('!cc add roll "a" | "b"', 'cc["add","roll"]~"\\"a\\" | \\"b\\""'),
        ('!cc add roll "say \\"hi\\""', 'cc["add","roll"]~"say \\\\\\"hi\\\\\\""'),
        ("!cc edit roll !random 1-{arg.1:int ?? 20}", 'cc["edit","roll"]~"!random 1-{arg.1:int ?? 20}"'),
        ("!cc", "cc[]"),
        ("!cc info roll", 'cc["info","roll"]'),
    ],
)
def test_raw_tail_forms(text: str, expected: str) -> None:
    assert line(text) == expected


def test_raw_tail_command_inside_group_is_rejected() -> None:
    assert error("( !explain x )").code is ParseErrorCode.RAW_TAIL_POSITION


def test_raw_tail_attempt_does_not_break_groups() -> None:
    assert line("( !a || true ) && !b") == "And(Group(Or(a[], true[])), b[])"
