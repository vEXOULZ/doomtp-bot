"""PEG recursive-descent parser mirroring command-language-spec Appendix C rule-for-rule (ADR-0011).

Each `_rule` method corresponds to the grammar rule of the same name. Conventions:

* Methods that may *fail* (PEG failure, triggers backtracking) return ``None`` and restore ``self.pos``.
* ``%E_CODE`` throws in the grammar are ``raise self._error(...)``; they are never caught by alternatives.
* Positions are 0-based character offsets into the pre-processed input; ``ParseError.column`` is 1-based.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from doomtp_bot.lang.ast import (
    And,
    Arg,
    Group,
    Invocation,
    Node,
    Or,
    Part,
    Pipe,
    Placeholder,
    Store,
    Text,
    TypeSpec,
    VarRef,
)
from doomtp_bot.lang.errors import ParseError, ParseErrorCode

MAX_EXPR_CHARS = 2000
MAX_PLACEHOLDER_NESTING = 4
MAX_NAME_CHARS = 32
MAX_VAR_NAME_CHARS = 32
DEFAULT_PREFIX = (
    "\U0001f3dc"  # \ud83c\udfdc \u2014 the default command sign; channels change it with `prefix`
)
VARIATION_SELECTOR = "\ufe0f"  # emoji presentation selector, optional around an emoji prefix

REGISTERED_ROOTS = frozenset(
    {
        "arg",
        "args",
        "chatter",
        "channel",
        "publisher",
        "cmd",
        "bot",
        "now",
        "event",
        "match",
        "cooldown",
        "denied",
        "run",
    }
)
TYPE_NAMES = (
    "str",
    "int",
    "float",
    "bool",
    "range",
    "duration",
    "user",
    "url",
)  # 'choice' handled separately
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
OPERATOR_TOKENS = ("||", "|", "&&", ">>", ">", "(", ")", ";")
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


class _Prefixed:
    """Mixin for the two places that match the channel prefix (LineStart and CmdPrefix)."""

    s: str
    n: int
    params: ParserParams

    def after_prefix(self, at: int) -> int | None:
        """Index just past the prefix at `at` (plus its optional gap), or None if it isn't there.

        U+FE0F is skipped on both sides, so a channel prefix saved as `\U0001f3dc` still matches the
        emoji-presentation `\U0001f3dc\ufe0f` that many chat clients send, and the other way round.
        """
        prefix, i, j = self.params.prefix, at, 0
        while j < len(prefix):
            if prefix[j] == VARIATION_SELECTOR:
                j += 1
            elif i < self.n and self.s[i] == VARIATION_SELECTOR:
                i += 1
            elif i < self.n and self.s[i] == prefix[j]:
                i, j = i + 1, j + 1
            else:
                return None
        while i < self.n and self.s[i] == VARIATION_SELECTOR:
            i += 1
        if allows_gap(prefix):
            while i < self.n and is_ws(self.s[i]):
                i += 1
        return i


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
    return _Parser(preprocess_line(text, reply_parent_login), ParserParams(prefix=prefix)).line_start()


def parse(text: str, context: Context, params: ParserParams) -> Node:
    """Parse `text` into an AST. Raises ParseError, or NotACommand for Line context.

    For Line context, `text` must already be pre-processed with `preprocess_line`.
    """
    if len(text) > MAX_EXPR_CHARS:
        raise ParseError(ParseErrorCode.TOO_LONG, MAX_EXPR_CHARS)
    parser = _Parser(text, params)
    if context is Context.LINE:
        if not parser.line_start():
            raise NotACommand
    else:
        parser.opt_ws()
    return _number_invocations(parser.line_body())


class _Parser(_Prefixed):
    def __init__(self, text: str, params: ParserParams) -> None:
        self.s = text
        self.n = len(text)
        self.pos = 0
        self.params = params
        self.depth = 0

    # ── primitives ──────────────────────────────────────────────────────────
    def _error(self, code: ParseErrorCode, offset: int | None = None, **fmt: str) -> ParseError:
        return ParseError(code, self.pos if offset is None else offset, **fmt)

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

    # ── C.2 entry points ────────────────────────────────────────────────────
    def line_start(self) -> bool:
        """LineStart <- &( (Open WS)* PREFIX PrefixGap? '@'? NameStart )"""
        at = 0
        while self.s.startswith("(", at) and at + 1 < self.n and is_ws(self.s[at + 1]):
            at += 1
            while at < self.n and is_ws(self.s[at]):
                at += 1
        end = self.after_prefix(at)
        if end is None:
            return False
        if self.s.startswith("@", end):
            end += 1
        return end < self.n and _is_name_start(self.s[end])

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

    # ── C.3 expressions ─────────────────────────────────────────────────────
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
        """Stage <- Primary StoreSuffix?    StoreSuffix <- WS StoreOp StoreTarget"""
        node = self.primary()
        save = self.pos
        if self.ws():
            op = ">>" if self.literal_op(">>") else ">" if self.literal_op(">") else None
            if op is not None:
                return Store(node, self.store_target(op), append=op == ">>")
        self.pos = save
        return node

    def store_target(self, op: str) -> VarRef:
        """StoreTarget <- WS VarRef / _ EOF %E_MISSING_OPERAND / WS OperatorToken %E_UNEXPECTED_OPERATOR"""
        self.require_operand(op)
        found = self.operator_at(self.pos)
        if found is not None:
            raise self._error(ParseErrorCode.UNEXPECTED_OPERATOR, op=found)
        return self.var_ref()

    def primary(self) -> Node:
        """Primary <- Group / &Reserved %RESERVED / &OperatorToken %UNEXPECTED / Invocation"""
        op = self.operator_at(self.pos)
        if op == "(":
            return self.group()
        if op is not None:
            raise self._operator_error(op)
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

    # ── C.4 invocations ─────────────────────────────────────────────────────
    def cmd_prefix(self) -> None:
        """CmdPrefix? <- PREFIX PrefixGap? &('@'? NameStart)"""
        end = self.after_prefix(self.pos)
        if end is None:
            return
        at = end + 1 if self.s.startswith("@", end) else end
        if at < self.n and _is_name_start(self.s[at]):
            self.pos = end

    def name(self) -> str:
        """Name <- NameStart NameChar* &(WSChar / EOF) / &'{' %E_DYNAMIC_NAME / %E_BAD_NAME"""
        start = self.pos
        if start < self.n and _is_name_start(self.s[start]):
            end = start + 1
            while end < self.n and _is_name_char(self.s[end]):
                end += 1
            if self.boundary(end) and end - start <= MAX_NAME_CHARS:
                self.pos = end
                return self.s[start:end].lower()
            raise self._error(ParseErrorCode.BAD_NAME, start)
        if self.s.startswith("{", start):
            raise self._error(ParseErrorCode.DYNAMIC_NAME, start)
        raise self._error(ParseErrorCode.BAD_NAME, start)

    def arg(self) -> Arg | None:
        """Arg <- WS !OperatorToken Word"""
        save = self.pos
        if self.ws() and not self.eof() and self.operator_at(self.pos) is None:
            return self.word()
        self.pos = save
        return None

    def invocation(self) -> Invocation:
        """Invocation <- CmdPrefix? '@'? Name Arg* RawCheck"""
        start = self.pos
        self.cmd_prefix()
        personal = self.s.startswith("@", self.pos)
        if personal:
            self.pos += 1
        name = self.name()
        args: list[Arg] = []
        while (a := self.arg()) is not None:
            args.append(a)
        # RawCheck: a raw-tail command inside a larger expression
        if isinstance(self.params.raw_tail_from(name, [_plain(a) for a in args]), int):
            raise self._error(ParseErrorCode.RAW_TAIL_POSITION, start, name=name)
        return Invocation(0, name, personal, tuple(args), None, (start, self.pos))

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
        while self.pos < self.n and not is_ws(self.s[self.pos]):
            ch = self.s[self.pos]
            if ch == '"':
                parts.extend(self.quoted())
            elif ch == "\\":
                parts.append(self.escape())
            elif ch == "{":
                parts.append(self.placeholder())
            else:
                start = self.pos
                while self.pos < self.n and not is_ws(self.s[self.pos]) and self.s[self.pos] not in '"\\{':
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
        """Placeholder <- '{' &{depth < MAX} (PhInner '}' / %E_BAD_PLACEHOLDER)"""
        start = self.pos
        if self.depth >= self.params.max_placeholder_nesting:
            raise self._error(ParseErrorCode.BAD_PLACEHOLDER, start)
        self.pos += 1
        self.depth += 1
        try:
            result = self._ph_inner(start)
            if result is None or not self.s.startswith("}", self.pos):
                raise self._error(ParseErrorCode.BAD_PLACEHOLDER, start + 1)
            self.pos += 1
            root, path, type_spec, fallback = result
            return Placeholder(root, path, type_spec, fallback, (start, self.pos))
        finally:
            self.depth -= 1

    def _ph_inner(
        self, start: int
    ) -> tuple[str, tuple[str, ...], TypeSpec | None, tuple[Part, ...] | None] | None:
        """PhInner <- _ Ref TypeSpec? Fallback? _"""
        self.opt_ws()
        ref = self._ref()
        if ref is None:
            return None
        root, path = ref
        type_spec: TypeSpec | None = None
        if self.s.startswith(":", self.pos):
            type_spec = self._type_spec()
            if type_spec is None:
                return None
        fallback = self._fallback()
        self.opt_ws()
        return root, path, type_spec, fallback

    def _ref(self) -> tuple[str, tuple[str, ...]] | None:
        """Ref <- Root ('.' Segment_)*"""
        s, at = self.s, self.pos
        if s.startswith("_", at) and not (at + 1 < self.n and _is_ident_char(s[at + 1])):
            root, at = "_", at + 1
        elif at < self.n and s[at] in "123456789":
            end = at + 1
            while end < self.n and s[end].isascii() and s[end].isdigit():
                end += 1
            root, at = s[at:end], end
        elif at < self.n and _is_ident_start(s[at]):
            end = at + 1
            while end < self.n and _is_ident_char(s[end]):
                end += 1
            root = s[at:end]
            if root not in self.params.registered_roots:
                return None
            at = end
        else:
            return None

        path: list[str] = []
        while s.startswith(".", at):
            seg_start = at + 1
            end = seg_start
            if end < self.n and s[end].isascii() and s[end].isdigit():
                while end < self.n and s[end].isascii() and s[end].isdigit():
                    end += 1
                if s.startswith("+raw", end):
                    end += 4
                elif s.startswith("+", end):
                    end += 1
            elif end < self.n and _is_ident_start(s[end]):
                while end < self.n and _is_ident_char(s[end]):
                    end += 1
            else:
                return None
            path.append(s[seg_start:end])
            at = end
        self.pos = at
        return root, tuple(path)

    def _type_spec(self) -> TypeSpec | None:
        """TypeSpec <- ':' _ (ChoiceType / TypeName)"""
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
                    return None
                choices.append(self.s[item_start : self.pos])
                self.opt_ws()
                if self.s.startswith(",", self.pos):
                    self.pos += 1
                    continue
                if self.s.startswith(")", self.pos):
                    self.pos += 1
                    return TypeSpec("choice", tuple(choices))
                return None
        for type_name in TYPE_NAMES:
            end = self.pos + len(type_name)
            if self.s.startswith(type_name, self.pos) and not (end < self.n and _is_ident_char(self.s[end])):
                self.pos = end
                return TypeSpec(type_name)
        return None

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

    # ── C.6 variable references ─────────────────────────────────────────────
    def var_ref(self) -> VarRef:
        """VarRef <- VarNs '.' VarName Boundary &{valid} / &'{' %E_DYNAMIC_VARREF / %E_BAD_VARREF"""
        start = self.pos
        for namespace in VAR_NAMESPACES:
            if self.s.startswith(namespace + ".", start):
                at = start + len(namespace) + 1
                end = at
                if end < self.n and "a" <= self.s[end] <= "z":
                    end += 1
                    while end < self.n and (
                        "a" <= self.s[end] <= "z" or "0" <= self.s[end] <= "9" or self.s[end] == "_"
                    ):
                        end += 1
                if end == at or not self.boundary(end):
                    break  # first alternative failed; fall through to the error alternatives
                var_name = self.s[at:end]
                if len(var_name) > MAX_VAR_NAME_CHARS or self.params.reserved_var_names(namespace, var_name):
                    raise self._error(ParseErrorCode.BAD_VARREF, start)
                self.pos = end
                return VarRef(namespace, var_name)
        if self.s.startswith("{", start):
            raise self._error(ParseErrorCode.DYNAMIC_VARREF, start)
        raise self._error(ParseErrorCode.BAD_VARREF, start)


# ── helpers ─────────────────────────────────────────────────────────────────
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
    """Assign 1-based pre-order indexes (spec §6.4)."""
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
        raise TypeError(f"not an AST node: {n!r}")

    return visit(node)
