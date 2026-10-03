"""Derived commands the bot ships, and the `core` system pack (ADR-0012 item 6, ADR-0019 "Sentinels").

A derived command is an ordinary custom command owned by the bot's account, written in the language
instead of Python. `scripts/starter_pack.py` installs them; nothing here writes to the database.

`core` is a **system pack**: its members resolve with the sentinels, before any other name, in every
channel, without being published, and nobody can toggle, shadow or publish them. The bot can't run
without it, so startup checks that the installed version is at least `CORE_VERSION`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from doomtp_bot.customcmds import params
from doomtp_bot.lang.ast import invocations

if TYPE_CHECKING:
    from doomtp_bot.customcmds.packs import PackService
    from doomtp_bot.lang.ast import Node
    from doomtp_bot.runtime.registry import CommandRegistry

CORE = "core"
CORE_SUMMARY = "Sentinels written in the language: they resolve everywhere and can't be switched off"
#: Bump whenever `CORE_COMMANDS` changes, so a bot started against an older install says so instead of
#: running without a sentinel it relies on.
CORE_VERSION = 1


@dataclass(frozen=True, slots=True)
class Derived:
    """One derived command, exactly as `!cc add`, `!cc describe` and `!cc param` would leave it."""

    name: str
    summary: str
    body: str
    #: Parameter declarations in the syntax `!cc param <name>` takes, minus the command name.
    declarations: tuple[str, ...] = field(default_factory=tuple)
    #: Printed after the install when the command needs something from the channel before it works.
    note: str = ""
    #: Callable only from the bodies of its own pack's commands (ADR-0019).
    internal: bool = False

    def params(self) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = []
        for declaration in self.declarations:
            position, _, rest = declaration.partition(" ")
            assignments, description = params.split_declaration(rest)
            rows = params.declare(rows, position, assignments, description)
        return tuple(rows)


CORE_COMMANDS: tuple[Derived, ...] = (
    Derived(name="false", summary="Always fails, with code 1 and no message", body="fail"),
    Derived(
        name="default",
        summary="Produce a fallback value: x || default none yet",
        body="echo {args}",
        declarations=('1+ name=value required=yes "the value to produce"',),
    ),
)


class CoreNotInstalled(RuntimeError):
    """Startup found no `core` pack, or an older one than this code needs."""


async def require_core(packs: PackService) -> bool:
    """Refuse to start without the `core` system pack at `CORE_VERSION` or later: without it, every
    expression using `false` or `default` would fail as an unknown command.

    Returns False instead on a database the bot has never signed in to: the pack script installs `core`
    under the bot's account, which only exists once the bot has run and been signed in at /auth/login.
    """
    pack = await packs.system_pack(CORE)
    installed = pack.system_version if pack is not None else None
    if installed is None and not await _signed_in(packs):
        return False
    if installed is None or installed < CORE_VERSION:
        found = "not installed" if installed is None else f"at version {installed}"
        raise CoreNotInstalled(
            f"the core pack is {found}, and this bot needs version {CORE_VERSION}: run python scripts/starter_pack.py"
        )
    return True


async def _signed_in(packs: PackService) -> bool:
    async with await packs.conn.execute("SELECT 1 FROM oauth_tokens WHERE identity = 'bot'") as cur:
        return await cur.fetchone() is not None


class NotASentinel(ValueError):
    """A system pack member would call something a channel can switch off or restrict."""


def check_sentinel_body(name: str, body: Node, registry: CommandRegistry, members: Iterable[str]) -> None:
    """A sentinel may be derived only if every command its body runs can never be disabled: a primitive
    sentinel (fixed policy, not toggleable) or another system pack member (ADR-0019)."""
    siblings = set(members)
    for inv in invocations(body):
        if inv.personal:
            raise NotASentinel(f"{name} calls a personal alias, @{inv.name}")
        if inv.name in siblings:
            continue
        command = registry.get(inv.name)
        if command is None or command.spec.toggleable or not command.spec.fixed_policy:
            raise NotASentinel(f"{name} calls {inv.name}, which isn't a sentinel")
