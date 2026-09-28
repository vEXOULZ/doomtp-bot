"""PEG recursive-descent parser mirroring command-language-spec Appendix C rule-for-rule (ADR-0011, ADR-0018).

Each `_rule` method corresponds to the grammar rule of the same name. Conventions:

* Methods that may *fail* (PEG failure, triggers backtracking) return ``None`` and restore ``self.pos``.
* ``%E_CODE`` throws in the grammar are ``raise self._error(...)``; they are never caught by alternatives.
* Positions are 0-based character offsets into the pre-processed input; ``ParseError.column`` is 1-based.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from doomtp_bot.lang.ast import (
    Access,
    And,
    Arg,
    Binary,
    Compare,
    Expr,
    Group,
    IfElse,
    Index,
    Invocation,
    Lit,
    Node,
    Or,
    Part,
    Pipe,
    Placeholder,
    Ref,
    Store,
    Subst,
    Text,
    Unary,
    VarRef,
    walk_expr,
)
from doomtp_bot.lang.errors import ParseError, ParseErrorCode

MAX_EXPR_CHARS = 2000
MAX_PLACEHOLDER_NESTING = 4
MAX_EXPR_DEPTH = 32  # parentheses, brackets, unary operators and placeholders, together
MAX_INT_DIGITS = 18
MAX_NAME_CHARS = 32
MAX_VAR_NAME_CHARS = 32
DEFAULT_PREFIX = (
    "\U0001f3dc"  # \ud83c\udfdc \u2014 the default command sign; channels change it with `prefix`
)
VARIATION_SELECTOR = "\ufe0f"  # emoji presentation selector, optional around an emoji prefix

# The fields the bot supplies, read as `{$root.field}` (ADR-0018 item 5). Read-only, and never a variable.
BOT_FIELDS: dict[str, frozenset[str]] = {
    "chatter": frozenset({"id", "name", "display", "rank", "roles", "is_sub", "is_vip", "is_mod"}),
    "channel": frozenset({"id", "name", "display", "prefix", "live", "title", "game", "viewers", "uptime"}),
    "publisher": frozenset({"id", "name", "display"}),
    "bot": frozenset({"name", "id", "version"}),
    "now": frozenset({"iso", "unix", "date", "time", "weekday"}),
}
# Roots a placeholder may start with, besides variables and `_`/`_N` (spec §2.7).
REGISTERED_ROOTS = frozenset(
    {"$" + root for root in BOT_FIELDS}
    | {"arg", "args", "cmd", "event", "match", "cooldown", "denied", "run"}
)
# Roots whose dots walk a structure the bot defines, as deep as it goes.
PATH_ROOTS = frozenset({"event", "match", "cooldown", "denied", "run", "cmd"})
RESULT_FIELDS = ("code", "message", "data")
# Accessors and casts after `:` (spec §2.7). `choice(a,b)` is handled separately.
ACCESSORS = ("len", "keys", "values")
TYPE_NAMES = ("str", "int", "float", "bool", "range", "duration", "user", "url", "list", "map")
# Commands whose arguments are one expression (ADR-0018 item 6).
EXPR_COMMANDS = frozenset({"check", "calc"})
# Longest first; each alternative requires a following '.' (spec §C.6 VarNs).
VAR_NAMESPACES = (
    "publisher.channel.chatter",
    "publisher.channel",
    "publisher.chatter",
    "publisher",
    "channel.chatter",
    "channel",
    "chatter",
)
# OperatorToken alternatives in grammar order; each must be followed by Boundary.
OPERATOR_TOKENS = ("||", "|", "&&", "-->", "->", "(", ")", ";")
# Inside an expression, longest first.
_COMPARE_OPS = ("==", "!=", "<=", ">=", "<", ">")
_UNKNOWN_OP = re.compile(r"\*\*|>>|<<|&&|\|\||\||&|\^|~|!(?!=)|=")
_NUMBER = re.compile(r"(\d+)(\.\d+)?([eE][+-]?\d+)?")
_KEYWORDS = frozenset({"true", "false", "not", "and", "or", "in"})
# Spec §2.1 step 1: invisible padding stripped from both ends of chat messages.
INVISIBLE_PADDING = frozenset({"\U000e0000", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"})
# str.isspace() also accepts U+001C..U+001F, which lack the Unicode White_Space property.
_NOT_WHITE_SPACE = frozenset("\x1c\x1d\x1e\x1f")


class Context(enum.StrEnum):
    LINE = "line"
    BODY = "body"
    TRIGGER = "trigger"
    LISTENER = "listener"
    CALLBACK = "callback"


class RawTail(enum.Enum):
    """Sentinels for `raw_tail_from` (spec §C.4). An int return means 'raw tail starts at argument N'."""

    MORE = "more"
    NONE = "none"


RawTailFn = Callable[[str, Sequence[str]], "int | RawTail"]


def _no_raw_tail(name: str, lead_args: Sequence[str]) -> int | RawTail:
    return RawTail.NONE


def _no_reserved_vars(namespace: str, name: str) -> bool:
    return False


@dataclass(frozen=True)
class ParserParams:
    """Runtime parameters for one parse call (spec §C.6)."""

    prefix: str = DEFAULT_PREFIX
    raw_tail_from: RawTailFn = _no_raw_tail
    registered_roots: frozenset[str] = field(default_factory=lambda: REGISTERED_ROOTS)
    reserved_var_names: Callable[[str, str], bool] = _no_reserved_vars
    max_placeholder_nesting: int = MAX_PLACEHOLDER_NESTING


def after_prefix(text: str, at: int, prefix: str) -> int | None:
    """Index just past `prefix` at `at` in `text` (plus its optional gap), or None if it isn't there.

    This is the one rule for matching the command sign (spec §2.1 PREFIX, PrefixGap): LineStart and
    CmdPrefix use it, and so does everything else that takes a sign off typed text.

    U+FE0F is skipped on both sides, so a channel prefix saved as `\U0001f3dc` still matches the
    emoji-presentation `\U0001f3dc\ufe0f` that many chat clients send, and the other way round.
    """
    i, j, n = at, 0, len(text)
    while j < len(prefix):
        if prefix[j] == VARIATION_SELECTOR:
            j += 1
        elif i < n and text[i] == VARIATION_SELECTOR:
            i += 1
        elif i < n and text[i] == prefix[j]:
            i, j = i + 1, j + 1
        else:
            return None
    while i < n and text[i] == VARIATION_SELECTOR:
        i += 1
    if allows_gap(prefix):
        while i < n and is_ws(text[i]):
            i += 1
    return i


def strip_prefix(text: str, prefix: str) -> str:
    """`text` without the command sign in front, matched as the parser matches it; unchanged without one."""
    end = after_prefix(text, 0, prefix)
    return text if end is None else text[end:]


class NotACommand(Exception):
    """Line context only: LineStart did not match; the message is ordinary chat (spec §2.1 step 4)."""


def is_ws(ch: str) -> bool:
    return ch.isspace() and ch not in _NOT_WHITE_SPACE


def _is_name_start(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def allows_gap(prefix: str) -> bool:
    """Emoji prefixes may be followed by a space: `\U0001f3dc ping` reads naturally, `! ping` does not
    (it would turn ordinary chat like "! that was close" into a command)."""
    stripped = prefix.rstrip(VARIATION_SELECTOR)
    return bool(stripped) and not stripped[-1].isascii()


def _is_name_char(ch: str) -> bool:
    return _is_name_start(ch) or ch in "_-"


def _is_ident_start(ch: str) -> bool:
    return ch.isascii() and (ch.isalpha() or ch == "_")


def _is_ident_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch == "_")


def _is_digit(ch: str) -> bool:
    return ch.isascii() and ch.isdigit()


def preprocess_line(text: str, reply_parent_login: str | Sequence[str] | None = None) -> str:
    """Spec §2.1 steps 1–3: strip invisible padding, the reply mention, and surrounding whitespace.

    `reply_parent_login` names the replied-to user: a login, or several names (login and display name).
    Twitch prefixes replies with `@DisplayName`, which can differ from the login beyond letter case.
    """
    start, end = 0, len(text)
    while start < end and (text[start] in INVISIBLE_PADDING or is_ws(text[start])):
        start += 1
    while end > start and (text[end - 1] in INVISIBLE_PADDING or is_ws(text[end - 1])):
        end -= 1
    text = text[start:end]

    names = [reply_parent_login] if isinstance(reply_parent_login, str) else list(reply_parent_login or ())
    for name in names:
        if not name:
            continue
        mention = "@" + name
        if (
            len(text) > len(mention)
            and text[: len(mention)].casefold() == mention.casefold()
            and is_ws(text[len(mention)])
        ):
            text = text[len(mention) :]
            break

    start, end = 0, len(text)
    while start < end and is_ws(text[start]):
        start += 1
    while end > start and is_ws(text[end - 1]):
        end -= 1
    return text[start:end]


def looks_like_command(text: str, prefix: str, reply_parent_login: str | Sequence[str] | None = None) -> bool:
    """Cheap Line-context check (spec §2.1 step 4): would this chat message be parsed as a command?"""
    return _Parser(
        preprocess_line(text, reply_parent_login), ParserParams(prefix=prefix), Context.LINE
    ).line_start()


def parse(text: str, context: Context, params: ParserParams) -> Node:
    """Parse `text` into an AST. Raises ParseError, or NotACommand for Line context.

    For Line context, `text` must already be pre-processed with `preprocess_line`.
    """
    if len(text) > MAX_EXPR_CHARS:
        raise ParseError(ParseErrorCode.TOO_LONG, MAX_EXPR_CHARS)
    parser = _Parser(text, params, context)
    if context is Context.LINE:
        if not parser.line_start():
            raise NotACommand
        if parser.expr_line is not None:  # `🏜1 + 3` runs as `calc` (ADR-0018 item 6)
            return Invocation(1, "calc", False, (), None, (0, len(text)), expr=parser.expr_line)
    else:
        parser.opt_ws()
    return _number_invocations(parser.line_body())


def parse_var_ref(text: str, params: ParserParams | None = None) -> VarRef:
    """A variable with an optional path, `channel.stats[kills]`, as `!var` takes it. `{!…}` isn't allowed."""
    parser = _Parser(text.strip(), params or ParserParams(), Context.BODY)
    ref = parser.var_ref()
    if not parser.eof():
        raise parser._error(ParseErrorCode.BAD_VARREF, 0)
    for key in ref.path:
        if any(isinstance(e, Subst) for e in walk_expr(key)):
            raise ParseError(ParseErrorCode.BAD_VARREF, 0, hint="a variable's path can't run a command")
    return ref


class _Parser:
    def __init__(self, text: str, params: ParserParams, context: Context = Context.BODY) -> None:
        self.s = text
        self.n = len(text)
        self.pos = 0
        self.params = params
        self.line = context is Context.LINE
        self.depth = 0  # placeholders
        self.expr_depth = 0
        self.in_subst = 0
        self.substs = 0  # `{!…}` invocations get -1, -2, … as they are met
        self.expr_line: Expr | None = None

    # ── primitives ──────────────────────────────────────────────────────────
    def _error(
        self, code: ParseErrorCode, offset: int | None = None, hint: str | None = None, **fmt: str
    ) -> ParseError:
        return ParseError(code, self.pos if offset is None else offset, hint, **fmt)

    def eof(self, at: int | None = None) -> bool:
        return (self.pos if at is None else at) >= self.n

    def boundary(self, at: int) -> bool:
        """Boundary <- &(WSChar / EOF)"""
        return at >= self.n or is_ws(self.s[at])

    def ws(self) -> bool:
        """WS <- WSChar+"""
        start = self.pos
        while self.pos < self.n and is_ws(self.s[self.pos]):
            self.pos += 1
        return self.pos > start

    def opt_ws(self) -> None:
        """_ <- WSChar*"""
        self.ws()

    def operator_at(self, at: int) -> str | None:
        """OperatorToken (lookahead): the operator starting at `at`, if any."""
        for op in OPERATOR_TOKENS:
            if self.s.startswith(op, at) and self.boundary(at + len(op)):
                return op
        return None

    def literal_op(self, op: str) -> bool:
        """Match `op Boundary` at the current position and consume `op`."""
        if self.s.startswith(op, self.pos) and self.boundary(self.pos + len(op)):
            self.pos += len(op)
            return True
        return False

    def subst_end(self, at: int) -> bool:
        """Inside `{!…}`, a `}` ends the command."""
        return self.in_subst > 0 and self.s.startswith("}", at)

    # ── C.2 entry points ────────────────────────────────────────────────────
    def line_start(self) -> bool:
        """LineStart <- &( (Open WS)* PREFIX PrefixGap? '@'? NameStart ) / &( PREFIX PrefixGap? ExprLine )"""
        at = 0
        while self.s.startswith("(", at) and at + 1 < self.n and is_ws(self.s[at + 1]):
            at += 1
            while at < self.n and is_ws(self.s[at]):
                at += 1
        end = after_prefix(self.s, at, self.params.prefix)
        if end is None:
            return False
        if at == 0 and end < self.n and (_is_digit(self.s[end]) or self.s[end] in "{(-"):
            self.expr_line = self._expression_line(end)
            if self.expr_line is not None:
                return True
        if self.s.startswith("@", end):
            end += 1
        if end >= self.n or not _is_name_start(self.s[end]):
            return False
        chunk = end
        while chunk < self.n and not is_ws(self.s[chunk]):
            chunk += 1
        return not self.s[end:chunk].isdigit()  # `🏜100` is chat, not a command named 100

    def _expression_line(self, at: int) -> Expr | None:
        """ExprLine <- Expr EOF, with at least one operator: a bare number or placeholder is not one."""
        save = self.pos
        self.pos = at
        try:
            expr = self.expression()
            self.opt_ws()
            if expr is None or not self.eof():
                return None
        except ParseError:
            return None
        finally:
            self.pos = save
        return expr if isinstance(expr, Unary | Binary | Compare) else None

    def line_body(self) -> Node:
        """LineBody <- RawTailInvocation _ EOF / Expr End"""
        save = self.pos
        raw = self.raw_tail_invocation()
        if raw is not None:
            self.opt_ws()
            if self.eof():
                return raw
        self.pos = save
        node = self.expr()
        self.end()
        return node

    def end(self) -> None:
        """End <- _ EOF / WS Reserved %RESERVED / WS Close %UNBALANCED / WS OperatorToken %UNEXPECTED"""
        self.opt_ws()
        if self.eof():
            return
        op = self.operator_at(self.pos)
        if op == ")":
            raise self._error(ParseErrorCode.UNBALANCED_GROUP)
        raise self._operator_error(op)

    def _operator_error(self, op: str | None) -> ParseError:
        """Reserved `;` → E_RESERVED_OPERATOR, any other operator → E_UNEXPECTED_OPERATOR."""
        if op == ";":
            return self._error(ParseErrorCode.RESERVED_OPERATOR)
        if op is not None:
            return self._error(ParseErrorCode.UNEXPECTED_OPERATOR, op=op)
        return self._error(ParseErrorCode.INTERNAL)

    def require_operand(self, op: str) -> None:
        """`_ EOF %E_MISSING_OPERAND`: skip spaces, and fail if the line ends where an operand must follow."""
        self.opt_ws()
        if self.eof():
            raise self._error(ParseErrorCode.MISSING_OPERAND, op=op)

    # ── C.3 command expressions ─────────────────────────────────────────────
    def expr(self) -> Node:
        return self.logical()

    def logical(self) -> Node:
        """Logical <- Pipeline (WS LogicOp LogicOperand)*"""
        left = self.pipeline()
        while True:
            save = self.pos
            if self.ws():
                op = "&&" if self.literal_op("&&") else "||" if self.literal_op("||") else None
                if op is not None:
                    right = self.logic_operand(op)
                    left = And(left, right) if op == "&&" else Or(left, right)
                    continue
            self.pos = save
            return left

    def logic_operand(self, op: str) -> Node:
        """LogicOperand <- WS Pipeline / _ EOF %E_MISSING_OPERAND"""
        self.require_operand(op)
        return self.pipeline()

    def pipeline(self) -> Node:
        """Pipeline <- Stage (WS PipeOp Operand)*"""
        left = self.stage()
        while True:
            save = self.pos
            if self.ws() and self.literal_op("|"):
                left = Pipe(left, self.operand("|"))
                continue
            self.pos = save
            return left

    def operand(self, op: str) -> Node:
        """Operand <- WS Stage / _ EOF %E_MISSING_OPERAND"""
        self.require_operand(op)
        return self.stage()

    def stage(self) -> Node:
        """Stage <- Primary StoreSuffix?    StoreSuffix <- WS StoreOp StoreTarget    StoreOp <- '-->' / '->'"""
        node = self.primary()
        save = self.pos
        if self.ws():
            op = "-->" if self.literal_op("-->") else "->" if self.literal_op("->") else None
            if op is not None:
                return Store(node, self.store_target(op), append=op == "-->")
        self.pos = save
        return node

    def store_target(self, op: str) -> VarRef:
        """StoreTarget <- WS VarRef Boundary / _ EOF %E_MISSING_OPERAND / WS OperatorToken %E_UNEXPECTED_OPERATOR"""
        self.require_operand(op)
        found = self.operator_at(self.pos)
        if found is not None:
            raise self._error(ParseErrorCode.UNEXPECTED_OPERATOR, op=found)
        start = self.pos
        target = self.var_ref()
        if not self.boundary(self.pos):
            raise self._error(ParseErrorCode.BAD_VARREF, start)
        return target

    def primary(self) -> Node:
        """Primary <- Group / &Reserved %RESERVED / &OperatorToken %UNEXPECTED / IfElse / Invocation"""
        op = self.operator_at(self.pos)
        if op == "(":
            return self.group()
        if op is not None:
            raise self._operator_error(op)
        ifelse = self.if_else()
        if ifelse is not None:
            return ifelse
        return self.invocation()

    def group(self) -> Group:
        """Group <- Open GroupInner GroupEnd"""
        self.pos += 1  # Open (boundary already checked by primary)
        # GroupInner <- WS Expr / _ EOF %E_MISSING_OPERAND
        self.require_operand("(")
        inner = self.expr()
        # GroupEnd <- WS Close / _ EOF %UNBALANCED / WS Reserved %RESERVED / WS OperatorToken %UNEXPECTED
        self.opt_ws()
        if self.eof():
            raise self._error(ParseErrorCode.UNBALANCED_GROUP)
        op = self.operator_at(self.pos)
        if op == ")":
            self.pos += 1
            return Group(inner)
        raise self._operator_error(op)

    def if_else(self) -> IfElse | None:
        """IfElse <- CmdPrefix? 'ifelse' WS Word WS Group (WS Group)?"""
        start = self.pos
        self.cmd_prefix()
        if not (self.s.startswith("ifelse", self.pos) and self.boundary(self.pos + len("ifelse"))):
            self.pos = start
            return None
        self.pos += len("ifelse")
        self.require_operand("ifelse")
        if self.operator_at(self.pos) is not None:
            raise self._error(ParseErrorCode.EXPR_SYNTAX, hint="ifelse needs a condition, then ( a command )")
        cond = self.word()
        self.require_operand("ifelse")
        if self.operator_at(self.pos) != "(":
            raise self._error(
                ParseErrorCode.EXPR_SYNTAX, hint="ifelse needs ( a command ) after its condition"
            )
        then = self.group()
        save = self.pos
        if self.ws() and self.operator_at(self.pos) == "(":
            return IfElse(cond, then, self.group())
        self.pos = save
        return IfElse(cond, then, None)

    # ── C.4 invocations ─────────────────────────────────────────────────────
    def cmd_prefix(self) -> None:
        """CmdPrefix? <- PREFIX PrefixGap? &('@'? NameStart)"""
        end = after_prefix(self.s, self.pos, self.params.prefix)
        if end is None:
            return
        at = end + 1 if self.s.startswith("@", end) else end
        if at < self.n and _is_name_start(self.s[at]):
            self.pos = end

    def name(self) -> str:
        """Name <- NameStart NameChar* &(WSChar / EOF) !Digits / &'{' %E_DYNAMIC_NAME / %E_BAD_NAME"""
        start = self.pos
        if start < self.n and _is_name_start(self.s[start]):
            end = start + 1
            while end < self.n and _is_name_char(self.s[end]):
                end += 1
            ok_end = self.boundary(end) or self.subst_end(end)
            if ok_end and end - start <= MAX_NAME_CHARS and not self.s[start:end].isdigit():
                self.pos = end
                return self.s[start:end].lower()
            raise self._error(ParseErrorCode.BAD_NAME, start)
        if self.s.startswith("{", start):
            raise self._error(ParseErrorCode.DYNAMIC_NAME, start)
        raise self._error(ParseErrorCode.BAD_NAME, start)

    def arg(self) -> Arg | None:
        """Arg <- WS !OperatorToken Word"""
        save = self.pos
        if (
            self.ws()
            and not self.eof()
            and self.operator_at(self.pos) is None
            and not self.subst_end(self.pos)
        ):
            start = self.pos
            word = self.word()
            if self.line and self.s[start : self.pos] in (">", ">>"):
                self._old_store(self.s[start : self.pos], start)
            return word
        self.pos = save
        return None

    def _old_store(self, op: str, at: int) -> None:
        """Typed lines only, for one release: `> channel.x` was a store before syntax 2.0 (ADR-0018)."""
        after = self.pos
        while after < self.n and is_ws(self.s[after]):
            after += 1
        if any(self.s.startswith(ns + ".", after) for ns in VAR_NAMESPACES):
            new = "->" if op == ">" else "-->"
            raise self._error(
                ParseErrorCode.UNEXPECTED_OPERATOR, at, hint=f"{op} is plain text now: store with {new}"
            )

    def invocation(self) -> Invocation:
        """Invocation <- CmdPrefix? '@'? Name (ExprArgs / Arg* RawCheck)"""
        start = self.pos
        self.cmd_prefix()
        personal = self.s.startswith("@", self.pos)
        if personal:
            self.pos += 1
        name = self.name()
        if not personal and name in EXPR_COMMANDS:
            return Invocation(0, name, personal, (), None, (start, self.pos), expr=self.expr_args())
        args: list[Arg] = []
        while (a := self.arg()) is not None:
            args.append(a)
        # RawCheck: a raw-tail command inside a larger expression
        if isinstance(self.params.raw_tail_from(name, [_plain(a) for a in args]), int):
            raise self._error(ParseErrorCode.RAW_TAIL_POSITION, start, name=name)
        return Invocation(0, name, personal, tuple(args), None, (start, self.pos))

    def expr_args(self) -> Expr | None:
        """ExprArgs <- WS Expression &(_ (EOF / OperatorToken / '}' in {!…})) / (nothing: a usage error later)"""
        save = self.pos
        if not self.ws() or self.eof() or self.operator_at(self.pos) is not None or self.subst_end(self.pos):
            self.pos = save
            return None
        start = self.pos
        expr = self.expression()
        if expr is None:
            raise self._error(ParseErrorCode.EXPR_SYNTAX, start)
        after = self.pos
        self.opt_ws()
        if self.eof() or self.operator_at(self.pos) is not None or self.subst_end(self.pos):
            self.pos = after
            return expr
        raise self._stray()

    def raw_tail_invocation(self) -> Invocation | None:
        """RawTailInvocation <- CmdPrefix? '@'? &NameStart Name &{raw != NONE} RawLeadArg* &{is_int} RawTail?"""
        start = self.pos
        self.cmd_prefix()
        personal = self.s.startswith("@", self.pos)
        if personal:
            self.pos += 1
        if self.eof() or not _is_name_start(self.s[self.pos]):
            self.pos = start
            return None
        name = self.name()
        raw_tail_from = self.params.raw_tail_from
        if raw_tail_from(name, []) is RawTail.NONE:
            self.pos = start
            return None

        args: list[Arg] = []
        plain: list[str] = []
        while self._needs_more_lead(name, plain):
            a = self.arg()
            if a is None:
                break
            args.append(a)
            plain.append(_plain(a))
        if not isinstance(raw_tail_from(name, plain), int):
            self.pos = start
            return None

        raw: str | None = None
        save = self.pos
        if self.ws() and not self.eof():
            raw = _strip_outer_quotes(self.s[self.pos :])
            self.pos = self.n
        else:
            self.pos = save
        return Invocation(0, name, personal, tuple(args), raw, (start, self.pos))

    def _needs_more_lead(self, name: str, lead: Sequence[str]) -> bool:
        r = self.params.raw_tail_from(name, lead)
        return r is RawTail.MORE or (isinstance(r, int) and len(lead) + 1 < r)

    # ── C.5 words, quotes, escapes, placeholders ────────────────────────────
    def word(self) -> Arg:
        """Word <- Segment+    Segment <- Quoted / Escape / Placeholder / Bare"""
        parts: list[Part] = []
        while self.pos < self.n and not is_ws(self.s[self.pos]) and not self.subst_end(self.pos):
            ch = self.s[self.pos]
            if ch == '"':
                parts.extend(self.quoted())
            elif ch == "\\":
                parts.append(self.escape())
            elif ch == "{":
                parts.append(self.placeholder())
            else:
                start = self.pos
                while (
                    self.pos < self.n
                    and not is_ws(self.s[self.pos])
                    and self.s[self.pos] not in '"\\{'
                    and not self.subst_end(self.pos)
                ):
                    self.pos += 1
                parts.append(Text(self.s[start : self.pos]))
        return _merge_text(parts)

    def quoted(self) -> list[Part]:
        """Quoted <- '"' QChar* ('"' / %E_UNTERMINATED_QUOTE)"""
        self.pos += 1
        parts: list[Part] = []
        while True:
            if self.eof():
                raise self._error(ParseErrorCode.UNTERMINATED_QUOTE)
            ch = self.s[self.pos]
            if ch == '"':
                self.pos += 1
                return parts
            if ch == "\\":
                parts.append(self.escape())
            elif ch == "{":
                parts.append(self.placeholder())
            else:
                start = self.pos
                while self.pos < self.n and self.s[self.pos] not in '"\\{':
                    self.pos += 1
                parts.append(Text(self.s[start : self.pos]))

    def escape(self) -> Text:
        """Escape <- '\\' . / '\\' EOF"""
        self.pos += 1
        if self.eof():
            return Text("\\")
        ch = self.s[self.pos]
        self.pos += 1
        return Text(ch)

    def placeholder(self) -> Placeholder:
        """Placeholder <- '{' &{depth < MAX} (Subst / PhInner '}' / %E_BAD_PLACEHOLDER)"""
        start = self.pos
        if self.depth >= self.params.max_placeholder_nesting:
            raise self._error(ParseErrorCode.BAD_PLACEHOLDER, start)
        self.pos += 1
        self.depth += 1
        self._deeper(start)
        try:
            if self.s.startswith("!", self.pos):
                return Placeholder(self.subst(start), None, (start, self.pos))
            if self.line:
                self._old_result_ref(start)
            self.opt_ws()
            expr = self.or_expr()
            if expr is None:
                raise self._error(ParseErrorCode.BAD_PLACEHOLDER, start + 1)
            fallback = self._fallback()
            self.opt_ws()
            if not self.s.startswith("}", self.pos):
                if self.eof():
                    raise self._error(ParseErrorCode.BAD_PLACEHOLDER, start + 1)
                raise self._stray()
            self.pos += 1
            return Placeholder(expr, fallback, (start, self.pos))
        finally:
            self.depth -= 1
            self.expr_depth -= 1

    def subst(self, start: int) -> Subst:
        """Subst <- '{!' Invocation _ '}'    (one command; its data, or else its message, is the value)"""
        self.pos += 1
        self.in_subst += 1
        try:
            inv = self.invocation()
        finally:
            self.in_subst -= 1
        self.opt_ws()
        if not self.s.startswith("}", self.pos):
            raise self._error(
                ParseErrorCode.BAD_PLACEHOLDER, start + 1, hint="{!…} runs one command and ends with }"
            )
        self.pos += 1
        self.substs += 1
        return Subst(dataclasses.replace(inv, index=-self.substs))

    def _old_result_ref(self, start: int) -> None:
        """Typed lines only, for one release: `{1}` and `{1.x}` were result refs before syntax 2.0."""
        m = re.compile(r"\s*([1-9]\d*)((?:\.\w+)*)\s*(\?\?|\})").match(self.s, self.pos)
        if m is None:
            return
        n, path = m.group(1), [p for p in m.group(2).split(".") if p]
        new = f"_{n}" + (
            "".join(f".{p}" for p in path)
            if all(p in RESULT_FIELDS for p in path)
            else "".join(f"[{p}]" for p in path)
        )
        raise self._error(ParseErrorCode.BAD_PLACEHOLDER, start, hint=f"a result is {{{new}}} now")

    def _fallback(self) -> tuple[Part, ...] | None:
        """Fallback <- _ '??' _ FbPart*    FbPart <- Escape / Placeholder / (!('{' / '}' / '\\') .)"""
        save = self.pos
        self.opt_ws()
        if not self.s.startswith("??", self.pos):
            self.pos = save
            return None
        self.pos += 2
        self.opt_ws()
        parts: list[Part] = []
        while self.pos < self.n and self.s[self.pos] != "}":
            ch = self.s[self.pos]
            if ch == "\\":
                parts.append(self.escape())
            elif ch == "{":
                parts.append(self.placeholder())
            else:
                start = self.pos
                while self.pos < self.n and self.s[self.pos] not in "{}\\":
                    self.pos += 1
                parts.append(Text(self.s[start : self.pos]))
        merged = list(_merge_text(parts))
        if merged and isinstance(merged[-1], Text):  # action: trim trailing WS
            trimmed = merged[-1].value.rstrip()
            if trimmed:
                merged[-1] = Text(trimmed)
            else:
                merged.pop()
        return tuple(merged)

    # ── C.7 expressions (ADR-0018) ──────────────────────────────────────────
    # Each rule returns None when nothing matches where an operand must start, and raises E_EXPR_SYNTAX
    # when an operator has no operand after it. Spaces between tokens are optional.
    def _deeper(self, at: int) -> None:
        self.expr_depth += 1
        if self.expr_depth > MAX_EXPR_DEPTH:
            raise self._error(ParseErrorCode.EXPR_TOO_DEEP, at)

    def _need(self, expr: Expr | None, at: int) -> Expr:
        if expr is None:
            raise self._error(ParseErrorCode.EXPR_SYNTAX, at)
        return expr

    def _stray(self) -> ParseError:
        """Something follows a complete expression: an operator we don't have, or anything else."""
        m = _UNKNOWN_OP.match(self.s, self.pos)
        if m is not None:
            return self._error(ParseErrorCode.UNKNOWN_OP, op=m.group())
        return self._error(ParseErrorCode.EXPR_SYNTAX)

    def _keyword(self, word: str) -> bool:
        end = self.pos + len(word)
        if self.s.startswith(word, self.pos) and not (end < self.n and _is_ident_char(self.s[end])):
            self.pos = end
            return True
        return False

    def _peek(self, *ops: str) -> str | None:
        """After optional spaces, the first of `ops` found (consumed with the spaces), else None."""
        save = self.pos
        self.opt_ws()
        for op in ops:
            if op.isalpha():
                if self._keyword(op):
                    return op
            elif self.s.startswith(op, self.pos):
                self.pos += len(op)
                return op
        self.pos = save
        return None

    def expression(self) -> Expr | None:
        """Expression <- OrExpr (_ '??' _ Expression)?"""
        left = self.or_expr()
        if left is None:
            return None
        at = self.pos
        if self._peek("??"):
            self.opt_ws()
            return Binary("??", left, self._need(self.expression(), at))
        return left

    def or_expr(self) -> Expr | None:
        """OrExpr <- AndExpr (_ 'or' _ AndExpr)*"""
        left = self.and_expr()
        while left is not None and (at := self.pos) is not None and self._peek("or"):
            self.opt_ws()
            left = Binary("or", left, self._need(self.and_expr(), at))
        return left

    def and_expr(self) -> Expr | None:
        """AndExpr <- NotExpr (_ 'and' _ NotExpr)*"""
        left = self.not_expr()
        while left is not None and (at := self.pos) is not None and self._peek("and"):
            self.opt_ws()
            left = Binary("and", left, self._need(self.not_expr(), at))
        return left

    def not_expr(self) -> Expr | None:
        """NotExpr <- 'not' _ NotExpr / Comparison"""
        at = self.pos
        if self._keyword("not"):
            self._deeper(at)
            try:
                self.opt_ws()
                return Unary("not", self._need(self.not_expr(), at))
            finally:
                self.expr_depth -= 1
        return self.comparison()

    def comparison(self) -> Expr | None:
        """Comparison <- Sum (_ CompareOp _ Sum)*    CompareOp <- == != <= >= < > in / not in"""
        first = self.sum()
        if first is None:
            return None
        rest: list[tuple[str, Expr]] = []
        while True:
            at = self.pos
            op = self._peek(*_COMPARE_OPS, "in", "not")
            if op is None:
                break
            if op in (">", "<") and self.s.startswith(op, self.pos):  # `>>` / `<<`
                self.pos = at
                break
            if op == "not":
                if not self._peek("in"):
                    self.pos = at
                    break
                op = "not in"
            self.opt_ws()
            rest.append((op, self._need(self.sum(), at)))
        return Compare(first, tuple(rest)) if rest else first

    def sum(self) -> Expr | None:
        """Sum <- Product (_ ('+' / '-') _ Product)*    (a `-` that starts `->` or `-->` ends the expression)"""
        left = self.product()
        while left is not None:
            at = self.pos
            self.opt_ws()
            if self.operator_at(self.pos) in ("->", "-->"):
                self.pos = at
                break
            self.pos = at
            op = self._peek("+", "-")
            if op is None:
                break
            self.opt_ws()
            left = Binary(op, left, self._need(self.product(), at))
        return left

    def product(self) -> Expr | None:
        """Product <- Unary (_ ('*' / '//' / '/' / '%') _ Unary)*"""
        left = self.unary()
        while left is not None:
            at = self.pos
            op = self._peek("//", "/", "%", "*")
            if op is None:
                break
            if op == "*" and self.s.startswith("*", self.pos):  # `**`
                self.pos = at
                break
            self.opt_ws()
            left = Binary(op, left, self._need(self.unary(), at))
        return left

    def unary(self) -> Expr | None:
        """Unary <- '-' _ Unary / Postfix"""
        at = self.pos
        if self.s.startswith("-", at) and self.operator_at(at) not in ("->", "-->"):
            self.pos += 1
            self._deeper(at)
            try:
                self.opt_ws()
                operand = self._need(self.unary(), at)
            finally:
                self.expr_depth -= 1
            if isinstance(operand, Lit) and type(operand.value) in (int, float):
                return Lit(-operand.value)
            return Unary("-", operand)
        return self.postfix()

    def postfix(self) -> Expr | None:
        """Postfix <- Atom (Accessor / '[' _ Key _ ']')*"""
        expr = self.atom()
        while expr is not None:
            if self.s.startswith(":", self.pos):
                expr = self.accessor(expr)
            elif self.s.startswith("[", self.pos):
                expr = Index(expr, self.bracket())
            else:
                break
        return expr

    def accessor(self, target: Expr) -> Access:
        """Accessor <- ':' _ ('choice(' Choices ')' / AccessorName / TypeName / %E_UNKNOWN_OP)"""
        at = self.pos
        self.pos += 1
        self.opt_ws()
        if self.s.startswith("choice(", self.pos):
            self.pos += len("choice(")
            choices: list[str] = []
            while True:
                self.opt_ws()
                item_start = self.pos
                while self.pos < self.n and self.s[self.pos] not in ",)}" and not is_ws(self.s[self.pos]):
                    self.pos += 1
                if self.pos == item_start:
                    raise self._error(ParseErrorCode.BAD_PLACEHOLDER, at, hint="choice needs its choices")
                choices.append(self.s[item_start : self.pos])
                self.opt_ws()
                if self.s.startswith(",", self.pos):
                    self.pos += 1
                    continue
                if self.s.startswith(")", self.pos):
                    self.pos += 1
                    return Access(target, "choice", tuple(choices))
                raise self._error(ParseErrorCode.BAD_PLACEHOLDER, at, hint="choice(a,b) ends with )")
        start = self.pos
        while self.pos < self.n and _is_ident_char(self.s[self.pos]):
            self.pos += 1
        name = self.s[start : self.pos]
        if name in ACCESSORS or name in TYPE_NAMES:
            return Access(target, name)
        raise self._error(ParseErrorCode.UNKNOWN_OP, at, op=":" + name)

    def bracket(self) -> Expr:
        """'[' _ (Ident &(_ ']') / Expression) _ ']'    A bare word is a key: `[kills]`, not a variable."""
        at = self.pos
        self.pos += 1
        self._deeper(at)
        try:
            self.opt_ws()
            key: Expr | None = None
            m = re.compile(r"([A-Za-z_]\w*)\s*\]").match(self.s, self.pos)
            if m is not None and not re.fullmatch(r"_\d*", m.group(1)):
                key = Lit(m.group(1))
                self.pos += len(m.group(1))
            else:
                key = self.expression()
            if key is None:
                raise self._error(ParseErrorCode.EXPR_SYNTAX, at)
            self.opt_ws()
            if not self.s.startswith("]", self.pos):
                raise self._stray() if not self.eof() else self._error(ParseErrorCode.EXPR_SYNTAX, at)
            self.pos += 1
            return key
        finally:
            self.expr_depth -= 1

    def atom(self) -> Expr | None:
        """Atom <- Number / String / 'true' / 'false' / '(' _ Expression _ ')' / Placeholder / Ref / VarRef"""
        at = self.pos
        if self.eof():
            return None
        ch = self.s[at]
        if _is_digit(ch):
            return self.number()
        if ch == '"':
            return Lit(self.string())
        if ch == "{":
            return self.placeholder()
        if ch == "(":
            self.pos += 1
            self._deeper(at)
            try:
                self.opt_ws()
                inner = self._need(self.expression(), self.pos)
                self.opt_ws()
                if not self.s.startswith(")", self.pos):
                    raise self._stray() if not self.eof() else self._error(ParseErrorCode.EXPR_SYNTAX, at)
                self.pos += 1
                return inner
            finally:
                self.expr_depth -= 1
        if self._keyword("true"):
            return Lit(True)
        if self._keyword("false"):
            return Lit(False)
        if ch == "$" or _is_ident_start(ch):
            return self.ref()
        return None

    def number(self) -> Lit:
        m = _NUMBER.match(self.s, self.pos)
        assert m is not None
        whole, frac, exp = m.groups()
        if frac is None and exp is None:
            if len(whole.lstrip("0")) > MAX_INT_DIGITS:
                raise self._error(
                    ParseErrorCode.EXPR_SYNTAX, hint=f"numbers have at most {MAX_INT_DIGITS} digits"
                )
            self.pos = m.end()
            return Lit(int(whole))
        value = float(m.group())
        if value in (float("inf"), float("-inf")):
            raise self._error(ParseErrorCode.EXPR_SYNTAX, hint="number too large")
        self.pos = m.end()
        return Lit(value)

    def string(self) -> str:
        """String <- '"' ('\\' . / !'"' .)* '"'    (literal: no placeholders inside)"""
        start = self.pos
        self.pos += 1
        out: list[str] = []
        while True:
            if self.eof():
                raise self._error(ParseErrorCode.UNTERMINATED_QUOTE, start)
            ch = self.s[self.pos]
            if ch == '"':
                self.pos += 1
                return "".join(out)
            if ch == "\\" and self.pos + 1 < self.n:
                out.append(self.s[self.pos + 1])
                self.pos += 2
                continue
            out.append(ch)
            self.pos += 1

    def _ident(self) -> str:
        start = self.pos
        while self.pos < self.n and _is_ident_char(self.s[self.pos]):
            self.pos += 1
        return self.s[start : self.pos]

    def _segments(self) -> list[str]:
        """('.' (Ident / Digits))*"""
        segments: list[str] = []
        while (
            self.s.startswith(".", self.pos)
            and self.pos + 1 < self.n
            and _is_ident_char(self.s[self.pos + 1])
        ):
            self.pos += 1
            segments.append(self._ident())
        return segments

    def ref(self) -> Expr | None:
        """Ref <- '$' BotRoot '.' Field / ResultRef / 'arg' '.' ArgSeg / 'args' / PathRoot Path / VarRef"""
        start = self.pos
        dollar = self.s.startswith("$", start)
        if dollar:
            self.pos += 1
        root = self._ident()
        if not dollar and root in _KEYWORDS:
            self.pos = start
            return None
        if dollar:
            if root not in BOT_FIELDS or "$" + root not in self.params.registered_roots:
                raise self._error(ParseErrorCode.BAD_PLACEHOLDER, start)
            fields = self._segments()
            if len(fields) != 1:
                raise self._error(
                    ParseErrorCode.BAD_PLACEHOLDER,
                    start,
                    hint=f"${root} takes one field, e.g. {{${root}.name}}",
                )
            return Ref("$" + root, (fields[0],))
        if re.fullmatch(r"_([1-9]\d*)?", root):
            path = self._segments()
            if len(path) > 1 or (path and path[0] not in RESULT_FIELDS):
                raise self._error(
                    ParseErrorCode.BAD_PLACEHOLDER,
                    start,
                    hint=f"a result has .code, .message and .data; use {root}[key]",
                )
            return Ref(root, tuple(path))
        if root in ("bot", "now"):
            raise self._error(
                ParseErrorCode.BAD_PLACEHOLDER, start, hint=f"the bot's fields start with $: {{${root}.…}}"
            )
        if root in ("chatter", "channel", "publisher"):
            self.pos = start
            return self._var_in_expr(start)
        if root not in self.params.registered_roots:
            self.pos = start
            return None
        if root == "args":
            return Ref("args")
        if root == "arg":
            return Ref("arg", (self._arg_segment(start),))
        return Ref(root, tuple(self._segments()))

    def _arg_segment(self, start: int) -> str:
        """ArgSeg <- '.' (Digits ('+raw' / '+' &End)? / Ident)    `{arg.1+1}` is a sum, `{arg.2+}` the rest."""
        m = re.compile(r"\.(?:(\d+)(\+raw(?!\w)|\+(?=[\s}:\])]|$))?|([A-Za-z_]\w*))").match(self.s, self.pos)
        if m is None:
            raise self._error(
                ParseErrorCode.BAD_PLACEHOLDER, start, hint="arg needs a number or name: {arg.1}"
            )
        self.pos = m.end()
        if self.s.startswith(".", self.pos):
            raise self._error(
                ParseErrorCode.BAD_PLACEHOLDER, start, hint="read inside a value with [ ]: {arg.1[key]}"
            )
        return m.group()[1:]

    def _var_in_expr(self, start: int) -> VarRef:
        """A variable read inside an expression. Typed lines point old `{chatter.name}` fields at `$`."""
        segments = [self._ident(), *self._segments()]
        ref = _split_var(segments)
        if self.line and len(segments) >= 2 and segments[1] in BOT_FIELDS.get(segments[0], ()):
            raise self._error(
                ParseErrorCode.BAD_PLACEHOLDER,
                start,
                hint=f"the bot's fields start with $: {{${segments[0]}.{segments[1]}}}",
            )
        if ref is None:
            if len(segments) > 2:
                ns = ".".join(segments[:2])
                raise self._error(
                    ParseErrorCode.BAD_PLACEHOLDER,
                    start,
                    hint=f"read inside a value with [ ]: {{{ns}[{']['.join(segments[2:])}]}}",
                )
            raise self._error(ParseErrorCode.BAD_PLACEHOLDER, start)
        namespace, name = ref
        if not _valid_var_name(name) or self.params.reserved_var_names(namespace, name):
            raise self._error(ParseErrorCode.BAD_PLACEHOLDER, start)
        return VarRef(namespace, name)

    # ── C.6 variable references ─────────────────────────────────────────────
    def var_ref(self) -> VarRef:
        """VarRef <- VarNs '.' VarName ('[' Key ']')* &{valid} / &'{' %E_DYNAMIC_VARREF / %E_BAD_VARREF"""
        start = self.pos
        if self.s.startswith("{", start):
            raise self._error(ParseErrorCode.DYNAMIC_VARREF, start)
        if not any(self.s.startswith(ns + ".", start) for ns in VAR_NAMESPACES):
            raise self._error(ParseErrorCode.BAD_VARREF, start)
        segments = [self._ident(), *self._segments()]
        ref = _split_var(segments)
        if ref is None:
            raise self._error(ParseErrorCode.BAD_VARREF, start)
        namespace, name = ref
        if not _valid_var_name(name) or self.params.reserved_var_names(namespace, name):
            raise self._error(ParseErrorCode.BAD_VARREF, start)
        path: list[Expr] = []
        while self.s.startswith("[", self.pos):
            path.append(self.bracket())
        return VarRef(namespace, name, tuple(path))


# ── helpers ─────────────────────────────────────────────────────────────────
def _split_var(segments: Sequence[str]) -> tuple[str, str] | None:
    """`["publisher", "channel", "wins"]` → ("publisher.channel", "wins"): the longest namespace, then one name."""
    for namespace in VAR_NAMESPACES:
        parts = namespace.split(".")
        if list(segments[: len(parts)]) == parts and len(segments) == len(parts) + 1:
            return namespace, segments[-1]
    return None


def _valid_var_name(name: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9_]*", name) is not None and len(name) <= MAX_VAR_NAME_CHARS


def _merge_text(parts: Sequence[Part]) -> Arg:
    merged: list[Part] = []
    for part in parts:
        if isinstance(part, Text) and merged and isinstance(merged[-1], Text):
            merged[-1] = Text(merged[-1].value + part.value)
        elif not (isinstance(part, Text) and part.value == ""):
            merged.append(part)
    return tuple(merged)


def _plain(arg: Arg) -> str:
    """Literal text of an argument for raw-tail subcommand matching; placeholders never match."""
    return "".join(p.value if isinstance(p, Text) else "\x00" for p in arg)


def _strip_outer_quotes(raw: str) -> str:
    """Spec §3.3 rule 3: remove one pair of outer quotes if there is no other unescaped quote inside."""
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw
    inner = raw[1:-1]
    escaped = False
    for ch in inner:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            return raw
    if escaped:  # the closing quote itself is escaped
        return raw
    return inner


def _number_invocations(node: Node) -> Node:
    """Assign 1-based pre-order indexes (spec §6.4). `{!…}` invocations keep their negative ones."""
    counter = 0

    def visit(n: Node) -> Node:
        nonlocal counter
        match n:
            case Invocation():
                counter += 1
                return dataclasses.replace(n, index=counter)
            case And(left, right):
                return And(visit(left), visit(right))
            case Or(left, right):
                return Or(visit(left), visit(right))
            case Pipe(left, right):
                return Pipe(visit(left), visit(right))
            case Group(inner):
                return Group(visit(inner))
            case Store(inner, target, append):
                return Store(visit(inner), target, append)
            case IfElse(cond, then, else_):
                then = Group(visit(then.inner))
                return IfElse(cond, then, Group(visit(else_.inner)) if else_ is not None else None)
        raise TypeError(f"not an AST node: {n!r}")

    return visit(node)
