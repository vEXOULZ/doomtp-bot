"""Parser behaviour beyond the conformance corpus: indexes, spans, limits, pre-processing, edge cases."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

import pytest

from doomtp_bot.lang.ast import (
    Index,
    Invocation,
    Lit,
    Pipe,
    Placeholder,
    Ref,
    Text,
    invocations,
    to_canonical,
)
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


PARAMS = ParserParams(  # the ASCII prefix; the emoji default gets its own tests below
    prefix="!", raw_tail_from=raw_tail_from, reserved_var_names=lambda ns, n: n in {"chatter", "channel"}
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
    node = parse("( !a || b ) && c | d -> channel.x", Context.LINE, PARAMS)
    assert [(i.index, i.name) for i in invocations(node)] == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]


def test_spans_cover_source() -> None:
    text = "!random 1-100 | echo {_1}!"
    node = parse(text, Context.LINE, PARAMS)
    assert isinstance(node, Pipe)
    first, second = invocations(node)
    assert text[first.span[0] : first.span[1]] == "!random 1-100"
    assert text[second.span[0] : second.span[1]] == "echo {_1}!"
    ph = second.args[0][0]
    assert isinstance(ph, Placeholder) and text[ph.span[0] : ph.span[1]] == "{_1}"


def _typed(inv: Invocation) -> list[tuple[str, str]]:
    """Each argument's source as (gap, text), placeholders shown by their own source."""
    return [
        (s.gap, "".join(p if isinstance(p, str) else f"<{p.span}>" for p in s.parts)) for s in inv.sources
    ]


def test_arguments_keep_their_source_for_raw() -> None:
    """`{arg.N+raw}` needs each argument as typed: its quotes, escapes and the spacing before it."""
    text = 'say  "a  b"   c\\"d  e'
    inv = parse(text, Context.BODY, PARAMS)
    assert isinstance(inv, Invocation)
    assert _typed(inv) == [("  ", '"a  b"'), ("   ", 'c\\"d'), ("  ", "e")]


def test_argument_sources_cut_around_their_placeholders() -> None:
    text = 'say x{arg.1}y  "q {arg.2 ?? {arg.3}} r"'
    inv = parse(text, Context.BODY, PARAMS)
    assert isinstance(inv, Invocation)
    (a, b) = inv.sources
    assert a.parts[0] == "x" and a.parts[2] == "y" and a.parts[1] is inv.args[0][1]
    ph = b.parts[1]
    assert b.gap == "  " and isinstance(ph, Placeholder) and b.parts[0] == '"q ' and b.parts[2] == ' r"'
    assert text[ph.span[0] : ph.span[1]] == "{arg.2 ?? {arg.3}}"  # the fallback goes with its placeholder


def test_raw_tail_lead_arguments_keep_their_source_too() -> None:
    inv = parse(preprocess_line('!cc  add   "x"  echo  hi'), Context.LINE, PARAMS)
    assert isinstance(inv, Invocation) and inv.raw_tail == "echo  hi"
    assert _typed(inv) == [("  ", "add"), ("   ", '"x"')]


def test_argument_sources_do_not_change_equality() -> None:
    inv = parse("say  a", Context.BODY, PARAMS)
    assert isinstance(inv, Invocation) and inv.sources
    assert dataclasses.replace(inv, sources=()) == inv


def test_names_are_case_folded() -> None:
    assert line("!RaNdOm 1-6") == 'random["1-6"]'


# ── pre-processing ─────────────────────────────────────────────────────────
def test_preprocess_strips_invisible_padding_and_whitespace() -> None:
    assert preprocess_line("\u200b  !ping \U000e0000") == "!ping"


def test_preprocess_keeps_inner_invisible_characters() -> None:
    assert preprocess_line("!echo a\u200bb") == "!echo a\u200bb"


def test_preprocess_reply_mention_uses_display_name_as_observed_live() -> None:
    # Live Twitch prefixes replies with the parent's display name, e.g. "@vexouLz pong".
    assert preprocess_line("@vexouLz !ping", ("vexouLz", "vexoulz")) == "!ping"
    # Localized display names differ from the login entirely.
    assert preprocess_line("@表示名 !ping", ("表示名", "tanaka_jp")) == "!ping"
    assert preprocess_line("@tanaka_jp !ping", ("表示名", "tanaka_jp")) == "!ping"
    assert preprocess_line("@someoneelse !ping", ("表示名", "tanaka_jp")) == "@someoneelse !ping"


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
    node = parse('!echo "it\'s {_1[celsius]}C now!"', Context.LINE, PARAMS)
    assert isinstance(node, Invocation)
    parts = node.args[0]
    assert parts[0] == Text("it's ") and isinstance(parts[1], Placeholder) and parts[2] == Text("C now!")
    assert parts[1].expr == Index(Ref("_1"), Lit("celsius"))


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
        ("!echo {_1[items][0]}", 'echo["{_1[items][0]}"]'),
        ("!echo { $chatter.name }", 'echo["{$chatter.name}"]'),
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
        "!echo {arg.}",
        "!echo {x}",
        "!echo {arg.1:choice()}",
        "!echo {_x}",
    ],
)
def test_bad_placeholders(text: str) -> None:
    assert error(text).code is ParseErrorCode.BAD_PLACEHOLDER


# ── syntax 1.0 spellings, rejected with the 2.0 one (ADR-0018) ─────────────
@pytest.mark.parametrize(
    ("text", "code", "hint"),
    [
        ("!echo {1}", ParseErrorCode.BAD_PLACEHOLDER, "a result is {_1} now"),
        (
            "!echo {chatter.display}",
            ParseErrorCode.BAD_PLACEHOLDER,
            "the bot's fields start with $: {$chatter.display}",
        ),
        ("!echo {_.x}", ParseErrorCode.BAD_PLACEHOLDER, "a result has .code, .message and .data; use _[key]"),
        ("!echo a > channel.x", ParseErrorCode.UNEXPECTED_OPERATOR, "> is plain text now: store with ->"),
        ("!echo a >> channel.x", ParseErrorCode.UNEXPECTED_OPERATOR, ">> is plain text now: store with -->"),
    ],
)
def test_v1_spellings_get_the_new_one(text: str, code: ParseErrorCode, hint: str) -> None:
    exc = error(text)
    assert (exc.code, exc.hint) == (code, hint)


def test_greater_than_is_text_in_bodies() -> None:
    assert body("echo {1} a>b") == 'echo["{1}","a>b"]'  # `{1}` is the number one
    assert body('echo ">" "->"') == 'echo[">","->"]'


# ── expressions (ADR-0018 item 3) ──────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("echo {-_1 * (2 + 3) // 4 % 5 - 1}", 'echo["{((((-_1) * (2 + 3)) // 4) % 5) - 1}"]'),
        ("echo {1 < channel.x <= 3}", 'echo["{1 < channel.x <= 3}"]'),
        (
            "echo {not arg.1 and $chatter.is_mod or false}",
            'echo["{((not arg.1) and $chatter.is_mod) or false}"]',
        ),
        (
            "echo {arg.1 in channel.list and 2 not in _1}",
            'echo["{(arg.1 in channel.list) and (2 not in _1)}"]',
        ),
        ("echo {channel.q[arg.1] ?? none}", 'echo["{channel.q[arg.1] ?? none}"]'),
        ("echo {arg.1 ?? 1 ?? 2}", 'echo["{arg.1 ?? 1 ?? 2}"]'),
        ('echo {channel.stats["best run"][-1]:len}', 'echo["{channel.stats[\\"best run\\"][-1]:len}"]'),
        ("echo {$now.date} {_2.code}", 'echo["{$now.date}","{_2.code}"]'),
        ("echo {!random 1-6} x{!echo {arg.1}}", 'echo["{!random[\\"1-6\\"]}","x{!echo[\\"{arg.1}\\"]}"]'),
        ("echo {channel.x:keys:len}", 'echo["{channel.x:keys:len}"]'),
    ],
)
def test_expressions(text: str, expected: str) -> None:
    assert body(text) == expected


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("echo {1 +}", ParseErrorCode.EXPR_SYNTAX),
        ("echo {1 2}", ParseErrorCode.EXPR_SYNTAX),
        ("echo {(1}", ParseErrorCode.EXPR_SYNTAX),
        ("echo {1 ** 2}", ParseErrorCode.UNKNOWN_OP),
        ("echo {arg.1:integer}", ParseErrorCode.UNKNOWN_OP),
        ("echo {" + "(" * 40 + "1" + ")" * 40 + "}", ParseErrorCode.EXPR_TOO_DEEP),
    ],
)
def test_expression_errors(text: str, code: ParseErrorCode) -> None:
    assert error(text, Context.BODY).code is code


def test_check_and_calc_take_one_expression() -> None:
    assert body("check 1 < channel.x < 3 && echo in") == 'And(check{1 < channel.x < 3}, echo["in"])'
    assert body("calc (1 + 2) * 3 -> channel.x") == "Store(calc{(1 + 2) * 3}, channel.x)"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!1 + 2", "calc{1 + 2}"),
        ("!(1+2)*3", "calc{(1 + 2) * 3}"),
        ("!{channel.x} * 2", "calc{{channel.x} * 2}"),
    ],
)
def test_bare_expression_lines_run_calc(text: str, expected: str) -> None:
    assert line(text) == expected


@pytest.mark.parametrize("text", ["!100", "!-1"])
def test_a_lone_number_is_not_a_command(text: str) -> None:
    with pytest.raises(NotACommand):
        parse(preprocess_line(text), Context.LINE, PARAMS)


def test_ifelse() -> None:
    assert (
        body("ifelse {_1.code == 0} ( echo a ) ( echo b ) -> channel.x")
        == 'Store(IfElse("{_1.code == 0}", Group(echo["a"]), Group(echo["b"])), channel.x)'
    )
    assert body("ifelse {channel.live} ( echo on )") == 'IfElse("{channel.live}", Group(echo["on"]))'


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("ifelse {1} echo a", ParseErrorCode.EXPR_SYNTAX),
        ("ifelse ( echo a )", ParseErrorCode.EXPR_SYNTAX),
        ("ifelse {1} ( echo a ) ( echo b ) ( echo c )", ParseErrorCode.UNEXPECTED_OPERATOR),
    ],
)
def test_ifelse_errors(text: str, code: ParseErrorCode) -> None:
    assert error(text, Context.BODY).code is code


def test_store_targets_take_a_path() -> None:
    assert body("echo a -> channel.x[arg.1][0]") == 'Store(echo["a"], channel.x[arg.1][0])'
    assert body("echo a --> channel.log[kills]") == 'Append(echo["a"], channel.log[kills])'


def test_placeholder_nesting_limit() -> None:
    ok = "!echo {chatter.a ?? {chatter.b ?? {chatter.c ?? {chatter.d ?? x}}}}"
    assert line(ok).startswith('echo["{chatter.a')
    too_deep = "!echo {chatter.a ?? {chatter.b ?? {chatter.c ?? {chatter.d ?? {chatter.e ?? x}}}}}"
    assert error(too_deep).code is ParseErrorCode.BAD_PLACEHOLDER


# ── store targets ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!a -> chatter.x", "Store(a[], chatter.x)"),
        ("!a -> publisher.chatterbox", "Store(a[], publisher.chatterbox)"),
        ("!a -> publisher.channel.round", "Store(a[], publisher.channel.round)"),
        ("( !a | b ) -> channel.x", "Store(Group(Pipe(a[], b[])), channel.x)"),
    ],
)
def test_store_targets(text: str, expected: str) -> None:
    assert line(text) == expected


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("!a -> channel.Deaths", ParseErrorCode.BAD_VARREF),
        ("!a -> channels.x", ParseErrorCode.BAD_VARREF),
        ("!a -> channel.", ParseErrorCode.BAD_VARREF),
        ("!a -> chatter.x" + "y" * 32, ParseErrorCode.BAD_VARREF),
        ("!a -> channel.x -> channel.y", ParseErrorCode.UNEXPECTED_OPERATOR),
        ("!a -> | b", ParseErrorCode.UNEXPECTED_OPERATOR),
        ("!a ->", ParseErrorCode.MISSING_OPERAND),
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
