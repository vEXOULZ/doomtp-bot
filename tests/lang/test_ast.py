from doomtp_bot.lang.ast import (
    Access,
    And,
    Binary,
    Compare,
    Group,
    IfElse,
    Index,
    Invocation,
    Lit,
    Or,
    Pipe,
    Placeholder,
    Ref,
    Store,
    Subst,
    Text,
    Unary,
    VarRef,
    invocations,
    render_expr,
    to_canonical,
)


def inv(index: int, name: str, *args: tuple, raw_tail: str | None = None) -> Invocation:  # type: ignore[type-arg]
    return Invocation(index, name, False, tuple(args), raw_tail, (0, 0))


def ph(expr, fallback=None) -> Placeholder:  # type: ignore[no-untyped-def]
    return Placeholder(expr, fallback, (0, 0))


def test_canonical_pipe_with_placeholder() -> None:
    node = Pipe(inv(1, "random", (Text("1-100"),)), inv(2, "echo", (Text("a"),), (ph(Ref("_1")), Text("!"))))
    assert to_canonical(node) == 'Pipe(random["1-100"], echo["a","{_1}!"])'


def test_canonical_precedence_shape() -> None:
    node = Or(And(inv(1, "a"), Pipe(inv(2, "b"), Store(inv(3, "c"), VarRef("channel", "x"), False))), inv(4, "d"))
    assert to_canonical(node) == "Or(And(a[], Pipe(b[], Store(c[], channel.x))), d[])"


def test_canonical_escapes_literal_braces_and_renders_casts_and_fallbacks() -> None:
    inner = ph(VarRef("channel", "location"), (Text("Lisbon"),))
    outer = ph(Access(Ref("arg", ("1",)), "choice", ("a", "b")), (inner,))
    node = inv(1, "echo", (Text("{x}"),), (outer,))
    assert to_canonical(node) == 'echo["\\\\{x\\\\}","{arg.1:choice(a,b) ?? {channel.location ?? Lisbon}}"]'


def test_canonical_raw_tail_and_group() -> None:
    node = And(Group(inv(1, "explain", raw_tail="!a | b")), inv(2, "b"))
    assert to_canonical(node) == 'And(Group(explain[]~"!a | b"), b[])'


def test_invocations_preorder() -> None:
    node = Or(And(inv(1, "a"), Pipe(inv(2, "b"), inv(3, "c"))), inv(4, "d"))
    assert [i.name for i in invocations(node)] == ["a", "b", "c", "d"]


def test_expressions_render_with_every_inner_operation_parenthesised() -> None:
    expr = Binary("-", Binary("*", Unary("-", Ref("_1")), Binary("+", Lit(2), Lit(3))), Lit(1))
    assert render_expr(expr) == "((-_1) * (2 + 3)) - 1"
    chained = Compare(Lit(1), (("<", VarRef("channel", "x")), ("<=", Lit(3))))
    assert render_expr(Binary("and", chained, Unary("not", Lit(False)))) == "(1 < channel.x <= 3) and (not false)"


def test_brackets_render_as_written() -> None:
    assert render_expr(Index(Ref("_1"), Lit("name"))) == "_1[name]"
    assert render_expr(Index(Ref("_1"), Lit("best run"))) == '_1["best run"]'
    assert render_expr(Index(Ref("_1"), Lit("_2"))) == '_1["_2"]'  # a bare `_2` would be a result
    assert render_expr(VarRef("channel", "log", (Lit(-1),))) == "channel.log[-1]"
    assert render_expr(Index(VarRef("channel", "q"), Ref("arg", ("1",)))) == "channel.q[arg.1]"


def test_substitution_and_ifelse_render() -> None:
    sub = Subst(inv(-1, "random", (Text("1-6"),)))
    assert to_canonical(inv(1, "echo", (ph(sub),))) == 'echo["{!random[\\"1-6\\"]}"]'
    node = IfElse((ph(Ref("$channel", ("live",))),), Group(inv(1, "a")), None)
    assert to_canonical(node) == 'IfElse("{$channel.live}", Group(a[]))'
