"""Rewrite syntax 1.0 text into syntax 2.0 (ADR-0018 item 7).

Stored bodies (custom commands, callbacks, automation) were written in 1.0. The rewrite is textual, so
spacing, quoting and escapes stay as the author wrote them; only the spellings that changed meaning move:

- result refs `{1}`, `{1.x}`, `{_.x}` → `{_1}`, `{_1[x]}`, `{_[x]}` (`.code`/`.message`/`.data` stay)
- bot fields `{chatter.display}`, `{bot.name}` → `{$chatter.display}`, `{$bot.name}`
- a path inside a value `{channel.stats.kills}` → `{channel.stats[kills]}`
- store and append `>` / `>>` → `->` / `-->`; a literal `->` or `-->` word is quoted so it stays text

Text that doesn't read as a 1.0 placeholder is copied unchanged. Run it once per body: a second run
would quote the `->` the first one wrote, which is why stored rows carry their syntax version.
"""

from __future__ import annotations

import re

from doomtp_bot.lang.parser import PATH_ROOTS, RESULT_FIELDS

# 1.0 context fields that shadowed variable names (the old namespaces.RESERVED_FIELDS, minus the
# structural `chatter`/`channel` segments, which start a longer namespace instead).
_V1_FIELDS: dict[str, frozenset[str]] = {
    "chatter": frozenset({"id", "name", "display", "rank", "roles", "is_sub", "is_vip", "is_mod"}),
    "channel": frozenset({"id", "name", "display", "prefix", "live", "title", "game", "viewers", "uptime"}),
    "publisher": frozenset({"id", "name", "display"}),
}
_V1_ROOTS = frozenset(
    {"arg", "args", "chatter", "channel", "publisher", "cmd", "bot", "now", *PATH_ROOTS}
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SEGMENT = re.compile(r"\d+(\+raw|\+)?|[A-Za-z_][A-Za-z0-9_]*")
_TYPE = re.compile(r":\s*(choice\([^)}]*\)|[a-z]+)")
_WS = " \t\n\r"


def migrate_v1(text: str) -> str:
    """The 2.0 spelling of a 1.0 body, expression or line."""
    out: list[str] = []
    i, n = 0, len(text)
    quoted = False
    while i < n:
        ch = text[i]
        if ch == "\\":
            out.append(text[i : i + 2])
            i += 2
        elif ch == '"':
            quoted = not quoted
            out.append(ch)
            i += 1
        elif ch == "{":
            i = _placeholder(text, i, out)
        elif not quoted and (i == 0 or text[i - 1] in _WS):
            end = i
            while end < n and text[end] not in _WS:
                end += 1
            word = text[i:end]
            if word == ">":
                out.append("->")
                i = end
            elif word == ">>":
                out.append("-->")
                i = end
            elif word in ("->", "-->"):
                out.append(f'"{word}"')
                i = end
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _placeholder(text: str, start: int, out: list[str]) -> int:
    """Rewrite the 1.0 placeholder at `start` into `out`; return where it ends. Not one → copy `{`."""
    found = _parse(text, start)
    if found is None:
        out.append("{")
        return start + 1
    end, rendered = found
    out.append(rendered)
    return end


def _parse(text: str, start: int) -> tuple[int, str] | None:
    """`{ _ Ref TypeSpec? Fallback? _ }` → (end, 2.0 text)."""
    n = len(text)
    i = start + 1
    lead = i
    while i < n and text[i] in _WS:
        i += 1
    ref = _ref(text, i)
    if ref is None:
        return None
    parts = ["{", text[lead:i]]
    i, root, path = ref
    parts.append(_ref_v2(root, path))
    m = _TYPE.match(text, i)
    if m is not None:
        parts.append(m.group())
        i = m.end()
    j = i
    while j < n and text[j] in _WS:
        j += 1
    if text.startswith("??", j):
        parts.append(text[i : j + 2])
        i = j + 2
        fallback: list[str] = []
        while i < n and text[i] != "}":
            if text[i] == "\\":
                fallback.append(text[i : i + 2])
                i += 2
            elif text[i] == "{":
                i = _placeholder(text, i, fallback)
            else:
                fallback.append(text[i])
                i += 1
        parts.append("".join(fallback))
    else:
        parts.append(text[i:j])
        i = j
    if i >= n or text[i] != "}":
        return None
    parts.append("}")
    return i + 1, "".join(parts)


def _joined(root: str, path: tuple[str, ...]) -> str:
    return ".".join((root, *path))


def _ref(text: str, i: int) -> tuple[int, str, tuple[str, ...]] | None:
    """1.0 `Ref <- Root ('.' Segment)*` at `i` → (end, root, path)."""
    n = len(text)
    if text.startswith("_", i) and not (i + 1 < n and (text[i + 1].isalnum() or text[i + 1] == "_")):
        root, i = "_", i + 1
    elif i < n and text[i] in "123456789":
        m = re.compile(r"\d+").match(text, i)
        assert m is not None
        root, i = m.group(), m.end()
    else:
        m = _IDENT.match(text, i)
        if m is None or m.group() not in _V1_ROOTS:
            return None
        root, i = m.group(), m.end()
    path: list[str] = []
    while text.startswith(".", i):
        m = _SEGMENT.match(text, i + 1)
        if m is None:
            return None
        path.append(m.group())
        i = m.end()
    return i, root, tuple(path)


def _ref_v2(root: str, path: tuple[str, ...]) -> str:
    if root == "_" or root.isdigit():
        head = "_" if root == "_" else f"_{root}"
        if len(path) == 1 and path[0] in RESULT_FIELDS:
            return f"{head}.{path[0]}"
        return head + _brackets(path)
    if root in ("bot", "now"):
        return f"${root}" + (f".{path[0]}" + _brackets(path[1:]) if path else "")
    if root in _V1_FIELDS:
        return _named(root, path)
    if root == "arg":
        return "arg" + (f".{path[0]}" + _brackets(path[1:]) if path else "")
    if root == "args":
        return "args" + _brackets(path)
    return _joined(root, path)  # PATH_ROOTS walk the bot's own structure with dots


def _named(root: str, path: tuple[str, ...]) -> str:
    """chatter/channel/publisher: a bot field (`$`) or a variable, as the 1.0 classifier split them."""
    if not path:
        return root
    head, *tail = path
    if head in _V1_FIELDS[root]:
        return f"${root}.{head}" + _brackets(tuple(tail))
    namespace, rest = root, list(path)
    for part in ("channel", "chatter"):
        if rest and len(rest) > 1 and rest[0] == part and f"{namespace}.{part}" in _NAMESPACES:
            namespace, rest = f"{namespace}.{part}", rest[1:]
    return f"{namespace}.{rest[0]}" + _brackets(tuple(rest[1:]))


_NAMESPACES = frozenset(
    {"channel.chatter", "publisher.channel", "publisher.chatter", "publisher.channel.chatter"}
)


def _brackets(path: tuple[str, ...]) -> str:
    """Steps into a value. A bare word is a key and digits an index; `_`/`_N` would read as a result."""
    return "".join(f'["{step}"]' if re.fullmatch(r"_\d*", step) else f"[{step}]" for step in path)
