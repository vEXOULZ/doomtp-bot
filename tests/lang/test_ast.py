from doomtp_bot.lang.ast import (
    And,
    Group,
    Invocation,
    Or,
    Pipe,
    Placeholder,
    Store,
    Text,
    TypeSpec,
    VarRef,
    invocations,
    to_canonical,
)


def inv(index: int, name: str, *args: tuple, raw_tail: str | None = None) -> Invocation:  # type: ignore[type-arg]
    return Invocation(index, name, False, tuple(args), raw_tail, (0, 0))


def test_canonical_pipe_with_placeholder() -> None:
    ph = Placeholder("1", (), None, None, (0, 0))
    node = Pipe(inv(1, "random", (Text("1-100"),)), inv(2, "echo", (Text("a"),), (ph, Text("!"))))
    assert to_canonical(node) == 'Pipe(random["1-100"], echo["a","{1}!"])'


def test_canonical_precedence_shape() -> None:
    node = Or(
        And(inv(1, "a"), Pipe(inv(2, "b"), Store(inv(3, "c"), VarRef("channel", "x"), False))), inv(4, "d")
    )
    assert to_canonical(node) == "Or(And(a[], Pipe(b[], Store(c[], channel.x))), d[])"


def test_canonical_escapes_literal_braces_and_renders_types_and_fallbacks() -> None:
    inner = Placeholder("channel", ("location",), None, (Text("Lisbon"),), (0, 0))
    outer = Placeholder("arg", ("1",), TypeSpec("choice", ("a", "b")), (inner,), (0, 0))
    node = inv(1, "echo", (Text("{x}"),), (outer,))
    assert to_canonical(node) == 'echo["\\\\{x\\\\}","{arg.1:choice(a,b) ?? {channel.location ?? Lisbon}}"]'


def test_canonical_raw_tail_and_group() -> None:
    node = And(Group(inv(1, "explain", raw_tail="!a | b")), inv(2, "b"))
    assert to_canonical(node) == 'And(Group(explain[]~"!a | b"), b[])'


def test_invocations_preorder() -> None:
    node = Or(And(inv(1, "a"), Pipe(inv(2, "b"), inv(3, "c"))), inv(4, "d"))
    assert [i.name for i in invocations(node)] == ["a", "b", "c", "d"]
