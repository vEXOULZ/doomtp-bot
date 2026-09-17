"""Built-in command registry: specs + handlers, name/alias lookup, raw-tail hook for the parser."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass

from doomtp_bot.lang.parser import RawTail
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.result import Result
from doomtp_bot.runtime.spec import CommandSpec

Handler = Callable[[CommandContext, Args, Result | None], Awaitable[Result]]


@dataclass(frozen=True, slots=True)
class Command:
    spec: CommandSpec
    handler: Handler
    # Raw-tail commands with subcommands: first argument -> raw tail position (spec §3.3).
    raw_tail_subcommands: tuple[tuple[str, int], ...] = ()


class CommandRegistry:
    def __init__(self, commands: Iterable[Command] = ()) -> None:
        self._by_name: dict[str, Command] = {}
        self._aliases: dict[str, str] = {}
        for command in commands:
            self.add(command)

    def add(self, command: Command) -> None:
        name = command.spec.name
        if name in self._by_name or name in self._aliases:
            raise ValueError(f"duplicate command name {name!r}")
        self._by_name[name] = command
        for alias in command.spec.aliases:
            if alias in self._by_name or alias in self._aliases:
                raise ValueError(f"duplicate command alias {alias!r}")
            self._aliases[alias] = name

    def extend(self, commands: Iterable[Command]) -> None:
        for command in commands:
            self.add(command)

    def get(self, name: str) -> Command | None:
        return self._by_name.get(self._aliases.get(name, name))

    def all(self) -> list[Command]:
        return sorted(self._by_name.values(), key=lambda c: c.spec.name)

    def raw_tail_from(self, name: str, lead_args: Sequence[str]) -> int | RawTail:
        """Parser hook (spec §C.4)."""
        command = self.get(name)
        if command is None:
            return RawTail.NONE
        if command.raw_tail_subcommands:
            if not lead_args:
                return RawTail.MORE
            for sub, position in command.raw_tail_subcommands:
                if lead_args[0].lower() == sub:
                    return position
            return RawTail.NONE
        return command.spec.raw_tail_from if command.spec.raw_tail_from is not None else RawTail.NONE


def command(
    spec: CommandSpec, *, raw_tail_subcommands: Sequence[tuple[str, int]] = ()
) -> Callable[[Handler], Command]:
    """Decorator turning a handler into a registrable Command."""

    def wrap(handler: Handler) -> Command:
        return Command(spec, handler, tuple(raw_tail_subcommands))

    return wrap
