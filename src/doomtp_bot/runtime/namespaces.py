"""Placeholder namespace registry in code form (docs/namespaces.md)."""

from __future__ import annotations

import re

from doomtp_bot.lang.parser import BOT_FIELDS, Context
from doomtp_bot.lang.parser import VAR_NAMESPACES as _GRAMMAR_NAMESPACES

# The grammar lists them longest-first for matching (spec §C.6); shortest-first reads better in help text.
VAR_NAMESPACES = tuple(reversed(_GRAMMAR_NAMESPACES))

# Which VarKey column holds the chatter's user id, for namespaces that have one.
CHATTER_KEY: dict[str, str] = {
    "chatter": "key1",
    "channel.chatter": "key2",
    "publisher.chatter": "key2",
    "publisher.channel.chatter": "key3",
}

# The bot's own fields are `$chatter.name` and so on (ADR-0018 item 5), so they never collide with a
# variable. What stays reserved is structural: a name that would read as a longer namespace
# (namespaces.md §5), and the result fields everywhere.
RESERVED_EVERYWHERE = frozenset({"data", "code", "message", "public", "root"})
_RESERVED_BY_NAMESPACE: dict[str, frozenset[str]] = {
    "channel": frozenset({"chatter"}),  # channel.chatter.x
    "publisher": frozenset({"chatter", "channel"}),  # publisher.chatter.x, publisher.channel.x
    "publisher.channel": frozenset({"chatter"}),  # publisher.channel.chatter.x
}


def is_reserved_var_name(namespace: str, name: str) -> bool:
    """Parser/store hook: variable names that would collide with longer namespaces or result fields (§5)."""
    return name in RESERVED_EVERYWHERE or name in _RESERVED_BY_NAMESPACE.get(namespace, frozenset())


# Roots available per context (spec §7.2). `_N` stands for every numbered result; variables are checked
# by the rules for their namespace instead.
_ALL = frozenset({"_", "_N", "$chatter", "$channel", "$bot", "$now", "run"})
CONTEXT_ROOTS: dict[Context, frozenset[str]] = {
    Context.LINE: _ALL,
    Context.BODY: _ALL | {"arg", "args", "$publisher", "cmd", "event"},
    Context.TRIGGER: _ALL | {"arg", "args", "event"},
    Context.LISTENER: _ALL | {"match"},
    Context.CALLBACK: _ALL | {"cooldown", "denied"},
}
_RESULT_REF = re.compile(r"_[1-9][0-9]*")


def root_available(root: str, context: Context) -> bool:
    key = "_N" if _RESULT_REF.fullmatch(root) else root
    return key in CONTEXT_ROOTS[context]


def bot_field_known(root: str, field: str) -> bool:
    """`$chatter.name` names a field the bot supplies; `$chatter.nmae` doesn't."""
    return field in BOT_FIELDS.get(root.removeprefix("$"), frozenset())
