import re

import pytest

from doomtp_bot.modules import builtin_registry
from doomtp_bot.runtime.result import MAX_MESSAGE_CHARS, Code, Result
from doomtp_bot.runtime.spec import CommandSpec, Example, Param, with_sign


def test_result_defaults_to_success() -> None:
    r = Result()
    assert r.ok and r.code == Code.OK and r.message is None and r.data is None


def test_result_truncates_long_messages() -> None:
    r = Result.success("x" * (MAX_MESSAGE_CHARS + 50))
    assert r.message is not None and len(r.message) == MAX_MESSAGE_CHARS and r.message.endswith("…")


def test_result_rejects_bad_codes() -> None:
    with pytest.raises(ValueError):
        Result(code=300)
    with pytest.raises(ValueError):
        Result.failure(Code.OK)


def test_spec_usage_text() -> None:
    spec = CommandSpec(
        name="roll",
        module="random",
        summary="Roll a die",
        params=(Param("1", "sides", type="int"), Param("2+", "label")),
    )
    assert spec.usage() == "roll [sides] [label…]"


@pytest.mark.parametrize(
    "params",
    [
        (Param("2", "a"),),  # not starting at 1
        (Param("1+", "a"), Param("2", "b")),  # variadic not last
        (Param("1", "a"), Param("2", "b", required=True)),  # required after optional
        (Param("1", "a"), Param("2", "a")),  # duplicate name
        (Param("1", "a", type="choice"),),  # choice without choices
    ],
)
def test_spec_rejects_invalid_params(params: tuple[Param, ...]) -> None:
    with pytest.raises(ValueError):
        CommandSpec(name="x", module="m", summary="s", params=params)


def test_no_spec_hard_codes_a_command_sign() -> None:
    """Channels pick their own sign, so spec text writes `{sign}` and never a literal one."""
    hard_coded = re.compile(r"(?<![\w`])!(?=[a-z])")
    offenders = []
    for cmd in builtin_registry().all():
        spec = cmd.spec
        texts = [spec.summary, spec.description, *(p.description for p in spec.params)]
        texts += [e.invocation for e in spec.examples] + [e.output for e in spec.examples]
        offenders += [(spec.name, t) for t in texts if hard_coded.search(t)]
    assert not offenders


def test_spec_text_is_rendered_with_the_reader_s_sign() -> None:
    example = Example("{sign}ping", "pong").rendered("?")
    assert example.invocation == "?ping"
    assert with_sign("type {sign}join", "\U0001f3dc") == "type \U0001f3dcjoin"
