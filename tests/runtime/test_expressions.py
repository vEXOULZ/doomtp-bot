"""Syntax 2.0 at run time (ADR-0018): expressions, operator commands, check/calc/ifelse, `{!…}`, errors."""

from __future__ import annotations

import pytest

from doomtp_bot.runtime.result import ErrorCode
from doomtp_bot.runtime.variables import InMemoryVariableStore, VarKey
from tests.runtime.helpers import ALICE, CHANNEL, make_runtime, run


async def said(text: str, store: InMemoryVariableStore | None = None) -> str | None:
    report = await run(make_runtime(store=store or InMemoryVariableStore()), text)
    assert report.result.code == 0, report.result
    return report.send


# ── expressions ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!echo {1 + 2 * 3} {(1 + 2) * 3} {-3 + 1}", "7 9 -2"),
        ("!echo {7 / 2} {7 // 2} {7 % 3} {6 / 3}", "3.5 3 1 2"),
        ("!echo {1 < 2 < 3} {3 < 2 < 4} {10 > 9}", "true false true"),
        ('!echo {"a" == "A"} {"10" > "9"} {"b" in "abc"}', "false true true"),
        ('!echo {not 0} {0 or 5} {3 and 4} {"x" not in "abc"}', "true 5 4 true"),
        ("!echo {$chatter.display} {$channel.name}", "Alice doomtp"),
        ("!echo {channel.who ?? nobody}", "nobody"),
    ],
)
async def test_expression_values(text: str, expected: str) -> None:
    assert await said(text) == expected


async def test_brackets_and_accessors_read_into_data() -> None:
    assert await said("!weather Lisbon | echo {_1[tags][-1]} {_1[tags]:len} {_1:keys}") == (
        "warm 2 celsius, location, tags"
    )
    assert await said('!echo {"abc":len}') == "3"


async def test_reading_past_the_end_is_missing_so_a_fallback_applies() -> None:
    assert await said("!weather Lisbon | echo {_1[tags][5] ?? none} {_1[nokey] ?? none}") == "none none"
    r = await run(make_runtime(), "!weather Lisbon | echo {_1[tags][5]}")
    assert (r.result.code, r.send) == (ErrorCode.E_MISSING_VALUE, "missing value: {_1[tags][5]}")


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("!echo {1 / 0}", ErrorCode.E_DIV_ZERO),
        ("!echo {1 // 0}", ErrorCode.E_DIV_ZERO),
        ("!echo {1 % 0}", ErrorCode.E_DIV_ZERO),
        ('!echo {"a" + 1}', ErrorCode.E_TYPE),
        ("!echo {5:len}", ErrorCode.E_TYPE),
        ("!weather Lisbon | echo {_1[celsius][0]}", ErrorCode.E_TYPE),
        ("!echo {999999999999999999 + 1}", ErrorCode.E_OVERFLOW),
        ("!echo {channel.missing}", ErrorCode.E_MISSING_VALUE),
        ("!echo {1 +}", ErrorCode.E_EXPR_SYNTAX),
        ("!echo {1 ** 2}", ErrorCode.E_UNKNOWN_OP),
        ("!echo {" + "(" * 40 + "1" + ")" * 40 + "}", ErrorCode.E_EXPR_TOO_DEEP),
        ("!echo {!echo {!echo {!echo {!echo deep}}}}", ErrorCode.E_SUBST_DEPTH),
    ],
)
async def test_every_failure_has_its_own_code(text: str, code: ErrorCode) -> None:
    r = await run(make_runtime(), text)
    assert r.result.code == code
    assert r.result.data["error"] == code.name  # type: ignore[index]


async def test_a_script_can_branch_on_the_error_code() -> None:
    assert await said("!echo {1 / 0} || echo {_.code == 233}") == "true"


# ── check, calc, bare expression lines, operator commands ──────────────────
async def test_check_branches_on_truth() -> None:
    assert await said("!check 1 < 2 && echo yes") == "yes"
    assert await said("!check 0 || echo no {_.code}") == "no 1"
    assert await said("!check {channel.nope} || echo {_.code}") == str(int(ErrorCode.E_MISSING_VALUE))


async def test_calc_and_bare_expression_lines() -> None:
    assert await said("!calc 2 * 21") == "42"
    assert await said("!1 + 3") == "4"
    assert await said("!(2 + 3) * 4") == "20"
    store = InMemoryVariableStore()
    store.data[VarKey("channel", "c1", name="deaths")] = 6
    assert await said("!{channel.deaths} * 2", store) == "12"


async def test_a_lone_number_is_not_a_command() -> None:
    runtime = make_runtime()
    assert await runtime.run("!100", runtime.make_context(channel=CHANNEL, invoker=ALICE)) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!add 1 2", "3"),
        ("!sub 5 7", "-2"),
        ("!mul 6 7", "42"),
        ("!div 7 2", "3.5"),
        ("!idiv 7 2", "3"),
        ("!mod 7 2", "1"),
        ("!neg 3", "-3"),
        ("!eq a a", "true"),
        ("!ne a b", "true"),
        ("!lt 2 10", "true"),  # numbers compare as numbers
        ("!ge 2 10", "false"),
        ("!in b abc", "true"),
        ("!not false", "true"),
        ("!and 1 0", "0"),
        ("!or 0 x", "x"),
    ],
)
async def test_operators_are_commands(text: str, expected: str) -> None:
    assert await said(text) == expected


# ── ifelse ─────────────────────────────────────────────────────────────────
async def test_ifelse_runs_only_the_chosen_branch() -> None:
    store = InMemoryVariableStore()
    assert await said("!ifelse {1 > 0} ( echo yes -> channel.a ) ( echo no -> channel.b )", store) == "yes"
    assert store.data == {VarKey("channel", "c1", name="a"): "yes"}
    assert await said("!ifelse {1 < 0} ( echo yes ) ( echo no )") == "no"
    assert await said("!ifelse {true} ( echo a ) ( fail 5 b ) | echo got {_}") == "got a"


async def test_ifelse_without_else_succeeds_quietly() -> None:
    r = await run(make_runtime(), "!ifelse {1 < 0} ( echo yes )")
    assert (r.result.code, r.send) == (0, None)


async def test_ifelse_on_a_missing_condition_fails_and_runs_nothing() -> None:
    r = await run(make_runtime(), "!ifelse {channel.nope} ( echo yes ) ( echo no )")
    assert (r.result.code, r.send) == (ErrorCode.E_MISSING_VALUE, "missing value: {channel.nope}")
    assert await said("!ifelse {channel.nope ?? false} ( echo yes ) ( echo no )") == "no"


@pytest.mark.parametrize("stored", ["false", "0", 0, False, [], {}])  # "" is missing (spec §7.3.3)
async def test_conditions_read_text_like_an_argument(stored: object) -> None:
    store = InMemoryVariableStore()
    store.data[VarKey("channel", "c1", name="t")] = stored
    assert await said("!ifelse {channel.t} ( echo yes ) ( echo no )", store) == "no"
    assert await said("!check {channel.t} || echo no", store) == "no"


# ── command substitution ───────────────────────────────────────────────────
async def test_substitution_is_the_commands_value() -> None:
    assert await said("!echo {!random 1-6} {!echo hi} {!add 2 3}") == "3 hi 5"
    assert await said("!echo {!echo {!echo {!echo three}}}") == "three"


async def test_a_failed_substitution_fails_the_invocation_with_its_code() -> None:
    r = await run(make_runtime(), "!echo {!fail 4 bad}")
    assert (r.result.code, r.send) == (4, "bad")
    r = await run(make_runtime(), "!echo {!foo}")
    assert (r.result.code, r.send) == (127, "unknown command: foo")


# ── stores into a path ─────────────────────────────────────────────────────
async def test_store_into_a_path_creates_maps() -> None:
    store = InMemoryVariableStore()
    assert await said("!echo a -> channel.m[x][y] && echo {channel.m} {channel.m[x][y]}", store) == (
        '{"x":{"y":"a"}} a'
    )
    assert await said("!echo a --> channel.l && echo b --> channel.l && echo {channel.l[-1]}", store) == "b"


async def test_store_into_a_non_collection_fails() -> None:
    r = await run(
        make_runtime(store=InMemoryVariableStore()), "!echo a -> channel.s && echo b -> channel.s[k]"
    )
    assert r.result.code == ErrorCode.E_NOT_A_MAP
