"""Parsing the IRC lines recent-messages returns (ADR-0008, RFC 2812 plus IRCv3 tags).

The service replays raw Twitch IRC, so this is a small, strict parser: tags, prefix, command, params.
Tags arrive in any order and the trailing parameter may be missing, which the tests cover.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TAG_ESCAPES = {r"\:": ";", r"\s": " ", r"\\": "\\", r"\r": "\r", r"\n": "\n"}


@dataclass(frozen=True, slots=True)
class IrcLine:
    command: str
    params: tuple[str, ...] = ()
    tags: dict[str, str] = field(default_factory=dict)
    prefix: str = ""

    @property
    def channel(self) -> str:
        return self.params[0].lstrip("#") if self.params else ""

    @property
    def text(self) -> str:
        return self.params[1] if len(self.params) > 1 else ""

    @property
    def nick(self) -> str:
        return self.prefix.split("!", 1)[0] if self.prefix else ""

    def tag(self, name: str, default: str = "") -> str:
        return self.tags.get(name, default)

    def tag_int(self, name: str, default: int = 0) -> int:
        raw = self.tags.get(name, "")
        try:
            return int(raw)
        except ValueError:
            return default


def unescape_tag(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        pair = value[i : i + 2]
        if pair in TAG_ESCAPES:
            out.append(TAG_ESCAPES[pair])
            i += 2
        elif value[i] == "\\":  # a lone trailing backslash is dropped, per IRCv3
            i += 1
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def parse_tags(raw: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in raw.split(";"):
        if not part:
            continue
        key, _, value = part.partition("=")
        tags[key] = unescape_tag(value)
    return tags


def parse_line(line: str) -> IrcLine | None:
    """One IRC line → IrcLine, or None if it is empty or malformed."""
    rest = line.strip("\r\n").strip()
    if not rest:
        return None
    tags: dict[str, str] = {}
    if rest.startswith("@"):
        raw_tags, _, rest = rest.partition(" ")
        tags = parse_tags(raw_tags[1:])
    prefix = ""
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    if not rest:
        return None
    command, _, remainder = rest.partition(" ")
    params: list[str] = []
    while remainder:
        if remainder.startswith(":"):
            params.append(remainder[1:])
            break
        head, _, remainder = remainder.partition(" ")
        params.append(head)
    return IrcLine(command.upper(), tuple(params), tags, prefix)


def badges(line: IrcLine) -> tuple[tuple[str, str], ...]:
    """`badges=moderator/1,subscriber/12` → (("moderator", "1"), ("subscriber", "12"))."""
    found: list[tuple[str, str]] = []
    for item in line.tag("badges").split(","):
        if not item:
            continue
        set_id, _, version = item.partition("/")
        found.append((set_id, version))
    return tuple(found)
