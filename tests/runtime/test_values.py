import pytest

from doomtp_bot.runtime.namespaces import FieldPath, VarPath, classify, is_reserved_var_name
from doomtp_bot.runtime.values import MISSING, ConversionError, convert, render


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (21.5, "21.5"),
        (3.0, "3"),
        (1 / 3, "0.333333"),
        (True, "true"),
        (None, ""),
        ([1, "a", 2.5], "1, a, 2.5"),
        ({"a": 1, "b": [1, 2]}, '{"a":1,"b":[1,2]}'),
        (-7, "-7"),
    ],
)
def test_render(value: object, text: str) -> None:
    assert render(value) == text


@pytest.mark.parametrize(
    ("raw", "type_name", "expected"),
    [
        (" 42 ", "int", 42),
        ("-3.5", "float", -3.5),
        ("YES", "bool", True),
        ("off", "bool", False),
        ("1-100", "range", {"lo": 1, "hi": 100}),
        ("1h30m", "duration", 5400),
        ("90", "duration", 90),
        ("https://example.com/x", "url", "https://example.com/x"),
    ],
)
async def test_convert_ok(raw: str, type_name: str, expected: object) -> None:
    assert await convert(raw, type_name) == expected


@pytest.mark.parametrize(
    ("raw", "type_name"),
    [
        ("4.2", "int"),
        ("nan", "float"),
        ("maybe", "bool"),
        ("9-1", "range"),
        ("", "duration"),
        ("ftp://x", "url"),
    ],
)
async def test_convert_errors(raw: str, type_name: str) -> None:
    with pytest.raises(ConversionError):
        await convert(raw, type_name)


async def test_convert_choice_and_bounds_and_user() -> None:
    assert await convert("LOUD", "choice", choices=("quiet", "loud")) == "loud"
    with pytest.raises(ConversionError):
        await convert("5", "int", maximum=4)

    async def resolve(login: str) -> dict[str, str] | None:
        return {"id": "9", "name": login, "display": login.title()} if login == "bob" else None

    assert (await convert("@Bob", "user", resolve_user=resolve))["id"] == "9"
    with pytest.raises(ConversionError):
        await convert("@nobody", "user", resolve_user=resolve)


def test_classify_placeholders() -> None:
    assert classify("chatter", ("name",)) == FieldPath("chatter", "name")
    assert classify("chatter", ("location", "city")) == VarPath("chatter", "location", ("city",))
    assert classify("channel", ("chatter", "points")) == VarPath("channel.chatter", "points")
    assert classify("publisher", ("channel", "chatter", "score")) == VarPath(
        "publisher.channel.chatter", "score"
    )
    assert classify("publisher", ("channel", "round")) == VarPath("publisher.channel", "round")
    assert classify("channel", ("chatter",)) is None
    assert MISSING is not None and not MISSING


def test_reserved_variable_names() -> None:
    assert is_reserved_var_name("chatter", "name")
    assert is_reserved_var_name("channel", "chatter")
    assert is_reserved_var_name("publisher", "channel")
    assert is_reserved_var_name("publisher.channel", "chatter")
    assert is_reserved_var_name("channel.chatter", "data")
    assert not is_reserved_var_name("channel.chatter", "name")
    assert not is_reserved_var_name("publisher", "chatterbox")
