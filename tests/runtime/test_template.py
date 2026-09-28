"""`:template` and `!customecho` (ADR-0019 item 8): a channel's own wording for a readout."""

from __future__ import annotations

from typing import Any

import pytest

from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import parse_template
from doomtp_bot.modules import builtin_registry
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.runtime.result import Code, ErrorCode
from doomtp_bot.runtime.variables import InMemoryVariableStore, VarKey
from tests.runtime.helpers import make_runtime, run

ECHOES = VarKey("channel", "c1", name="customecho")
READOUT = "!echo {channel.customecho[lurk]:template ?? thanks for the lurk, {$chatter.display}}"


def store_with(**echoes: Any) -> InMemoryVariableStore:
    store = InMemoryVariableStore()
    if echoes:
        store.data[ECHOES] = echoes
    return store


async def said(text: str, store: InMemoryVariableStore) -> str | None:
    report = await run(make_runtime(store=store), text)
    assert report.result.code == 0, report.result
    return report.send


# ── :template ───────────────────────────────────────────────────────────────
async def test_a_readout_falls_back_to_its_own_wording() -> None:
    assert await said(READOUT, store_with()) == "thanks for the lurk, Alice"


async def test_a_stored_template_is_rendered_where_it_is_read() -> None:
    store = store_with(lurk="{$chatter.display} went for snacks \\{brb} in {$channel.display}}")
    assert await said(READOUT, store) == "Alice went for snacks {brb} in DoomTP}"


async def test_a_template_missing_a_value_gives_way_to_the_fallback() -> None:
    assert (
        await said(READOUT, store_with(lurk="lurking with {channel.nothing}")) == "thanks for the lurk, Alice"
    )
    assert await said(READOUT, store_with(lurk="lurking with {channel.nothing ?? friends}")) == (
        "lurking with friends"
    )


@pytest.mark.parametrize(
    ("stored", "code"),
    [
        (5, ErrorCode.E_TYPE),
        ("{!echo hi}", ErrorCode.E_BAD_PLACEHOLDER),
        ("{channel.x:template}", ErrorCode.E_BAD_PLACEHOLDER),
        ("{channel.x ?? {!echo hi}}", ErrorCode.E_BAD_PLACEHOLDER),
        ("{1 +}", ErrorCode.E_EXPR_SYNTAX),
    ],
)
async def test_a_template_that_cant_render_fails_with_its_error(stored: Any, code: ErrorCode) -> None:
    report = await run(make_runtime(store=store_with(lurk=stored)), READOUT)
    assert report.result.code == code


def test_parse_template_keeps_text_and_placeholders_apart() -> None:
    parts = parse_template("hi {$chatter.display}, } stays")
    assert [type(p).__name__ for p in parts] == ["Text", "Placeholder", "Text"]
    with pytest.raises(ParseError):
        parse_template("unclosed {channel.x")


# ── !customecho ─────────────────────────────────────────────────────────────
async def echo_run(text: str, store: InMemoryVariableStore) -> tuple[int, str | None]:
    report = await run(Runtime(builtin_registry(), store=store), text)
    return report.result.code, report.send


async def test_customecho_stores_the_template_as_typed() -> None:
    store = store_with()
    code, send = await echo_run("!customecho set lurk {$chatter.display} is off to {arg.1 ?? nap}", store)
    assert (code, send) == (0, "lurk now says: {$chatter.display} is off to {arg.1 ?? nap}")
    assert store.data[ECHOES] == {"lurk": "{$chatter.display} is off to {arg.1 ?? nap}"}
    assert await said(READOUT, store) == "Alice is off to nap"
    assert await echo_run("!customecho show !lurk", store) == (
        0,
        "lurk: {$chatter.display} is off to {arg.1 ?? nap}",
    )


async def test_customecho_clear_goes_back_to_the_readouts_own_wording() -> None:
    store = store_with(lurk="bye", uptime="up")
    assert await echo_run("!customecho clear lurk", store) == (0, "lurk is back to its own wording")
    assert store.data[ECHOES] == {"uptime": "up"}
    assert await echo_run("!customecho clear lurk", store) == (Code.NOT_FOUND, "lurk has no custom wording")
    assert await echo_run("!customecho show lurk", store) == (Code.NOT_FOUND, "lurk has no custom wording")


@pytest.mark.parametrize(
    "text",
    ["!customecho set lurk {!echo hi}", "!customecho set lurk {x:template}", "!customecho set lurk {oops"],
)
async def test_customecho_refuses_a_template_that_would_not_render(text: str) -> None:
    store = store_with()
    code, send = await echo_run(text, store)
    assert code != 0 and send is not None and send.startswith("parse error")
    assert ECHOES not in store.data
