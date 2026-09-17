"""Placeholder namespace registry in code form (docs/namespaces.md)."""

from __future__ import annotations

from dataclasses import dataclass

from doomtp_bot.lang.parser import Context

VAR_NAMESPACES = (
    "chatter",
    "channel",
    "channel.chatter",
    "publisher",
    "publisher.chatter",
    "publisher.channel",
    "publisher.channel.chatter",
)

# Read-only context fields that shadow variable names (namespaces.md §4).
RESERVED_FIELDS: dict[str, frozenset[str]] = {
    "chatter": frozenset({"id", "name", "display", "rank", "roles", "is_sub", "is_vip", "is_mod"}),
    "channel": frozenset(
        {"id", "name", "display", "prefix", "live", "title", "game", "viewers", "uptime", "chatter"}
    ),
    "publisher": frozenset({"id", "name", "display", "chatter", "channel"}),
}
# Names that can never be variable names in any namespace (namespaces.md §5).
RESERVED_EVERYWHERE = frozenset({"data", "code", "message", "public", "root"})


_RESERVED_BY_NAMESPACE: dict[str, frozenset[str]] = {
    "chatter": RESERVED_FIELDS["chatter"],
    "channel": RESERVED_FIELDS["channel"],  # includes "chatter" (channel.chatter.x)
    "publisher": RESERVED_FIELDS["publisher"],  # includes "chatter" and "channel"
    "publisher.channel": frozenset({"chatter"}),  # publisher.channel.chatter.x
}


def is_reserved_var_name(namespace: str, name: str) -> bool:
    """Parser/store hook: variable names that would collide with fields or longer namespaces (§5)."""
    return name in RESERVED_EVERYWHERE or name in _RESERVED_BY_NAMESPACE.get(namespace, frozenset())


# Roots available per context (spec §7.2). Variables/fields under chatter/channel are checked separately.
_ALL = frozenset({"_", "N", "chatter", "channel", "bot", "now", "run"})
CONTEXT_ROOTS: dict[Context, frozenset[str]] = {
    Context.LINE: _ALL,
    Context.BODY: _ALL | {"arg", "args", "publisher", "cmd", "event"},
    Context.TRIGGER: _ALL | {"arg", "args", "event"},
    Context.LISTENER: _ALL | {"match"},
    Context.CALLBACK: _ALL | {"cooldown", "denied"},
}


def root_available(root: str, context: Context) -> bool:
    key = "N" if root.isdigit() else root
    return key in CONTEXT_ROOTS[context]


@dataclass(frozen=True, slots=True)
class VarPath:
    """A placeholder or store target that addresses a variable: namespace, name, and a path inside its value."""

    namespace: str
    name: str
    rest: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldPath:
    """A placeholder that addresses a reserved context field."""

    root: str
    field: str
    rest: tuple[str, ...] = ()


def classify(root: str, path: tuple[str, ...]) -> VarPath | FieldPath | None:
    """Split `{root.path…}` for chatter/channel/publisher roots into a variable or a field reference."""
    if root not in ("chatter", "channel", "publisher") or not path:
        return None
    head, *tail = path
    if root == "chatter":
        if head in RESERVED_FIELDS["chatter"]:
            return FieldPath("chatter", head, tuple(tail))
        return VarPath("chatter", head, tuple(tail))
    if root == "channel":
        if head == "chatter":
            if not tail:
                return None
            return VarPath("channel.chatter", tail[0], tuple(tail[1:]))
        if head in RESERVED_FIELDS["channel"]:
            return FieldPath("channel", head, tuple(tail))
        return VarPath("channel", head, tuple(tail))
    # publisher
    if head == "chatter":
        return VarPath("publisher.chatter", tail[0], tuple(tail[1:])) if tail else None
    if head == "channel":
        if not tail:
            return None
        if tail[0] == "chatter":
            return VarPath("publisher.channel.chatter", tail[1], tuple(tail[2:])) if len(tail) > 1 else None
        return VarPath("publisher.channel", tail[0], tuple(tail[1:]))
    if head in RESERVED_FIELDS["publisher"]:
        return FieldPath("publisher", head, tuple(tail))
    return VarPath("publisher", head, tuple(tail))
