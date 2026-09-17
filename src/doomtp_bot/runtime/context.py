"""Execution context: who is running what, where, with which services (spec §6, ADR-0005)."""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from doomtp_bot.lang.parser import Context
from doomtp_bot.runtime.values import UserResolver

if TYPE_CHECKING:
    from doomtp_bot.runtime.result import Result
    from doomtp_bot.runtime.variables import VariableSession


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    id: str
    login: str
    display: str = ""
    prefix: str = "!"
    timezone: str = "UTC"
    live: bool = False
    title: str = ""
    game: str = ""
    viewers: int = 0
    started_at: float | None = None  # unix seconds
    quiet_errors: bool = False
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Chatter:
    id: str
    login: str
    display: str = ""
    badges: frozenset[str] = frozenset()  # badge set_ids, e.g. {"moderator", "subscriber"}
    roles: tuple[str, ...] = ("everyone",)
    rank: int = 0

    @property
    def is_sub(self) -> bool:
        return "subscriber" in self.roles

    @property
    def is_vip(self) -> bool:
        return "vip" in self.roles

    @property
    def is_mod(self) -> bool:
        return self.rank >= 80


@dataclass(frozen=True, slots=True)
class Publisher:
    """Owner of the running custom command (Body context)."""

    id: str
    login: str
    display: str = ""
    command_id: str = ""
    command_name: str = ""
    alias: str = ""
    version: int = 0


class RunCancelled(Exception):
    """Moderation cancelled the run (code 130, spec §6.7)."""


@dataclass
class ExecContext:
    """Everything an expression evaluation needs. One instance per run (shared by nested scopes)."""

    context: Context
    channel: ChannelInfo
    invoker: Chatter | None
    variables: VariableSession
    trigger_type: str = "chat"
    trigger_id: str | None = None
    message_id: str | None = None
    message_sent_at: float | None = None
    publisher: Publisher | None = None
    event: dict[str, Any] = field(default_factory=dict)
    match: dict[str, Any] = field(default_factory=dict)
    cooldown: dict[str, Any] = field(default_factory=dict)
    denied: dict[str, Any] = field(default_factory=dict)
    bot: dict[str, Any] = field(default_factory=dict)
    resolve_user: UserResolver | None = None
    run_as_rank: int | None = (
        None  # triggers run at a fixed rank instead of the event user's (architecture §7)
    )
    in_callback: bool = False  # callbacks never trigger other callbacks
    services: dict[str, Any] = field(default_factory=dict)  # e.g. "policy", "variables_admin", "twitch"
    is_cancelled: Callable[[], bool] = lambda: False
    rng: random.Random = field(default_factory=random.Random)
    clock: Callable[[], float] = time.time
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def ensure_not_cancelled(self) -> None:
        if self.is_cancelled():
            raise RunCancelled

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=UTC)


@dataclass(frozen=True, slots=True)
class Args:
    """Arguments delivered to a command handler after expansion and validation (spec §5.3)."""

    values: tuple[str, ...]
    params: dict[str, Any]
    raw_tail: str | None = None

    def __getitem__(self, name: str) -> Any:
        return self.params[name]

    def get(self, name: str, default: Any = None) -> Any:
        value = self.params.get(name)
        return default if value is None else value

    def rest(self, start: int = 1) -> str:
        """Arguments from 1-based position `start` joined with single spaces."""
        return " ".join(self.values[start - 1 :])


@dataclass
class CommandContext:
    """What a command handler sees."""

    exec: ExecContext
    invocation_index: int
    name: str
    prev: Result | None = None  # the Result visible as {_} (spec §6.4)

    @property
    def channel(self) -> ChannelInfo:
        return self.exec.channel

    @property
    def invoker(self) -> Chatter | None:
        return self.exec.invoker

    @property
    def rng(self) -> random.Random:
        return self.exec.rng

    @property
    def variables(self) -> VariableSession:
        return self.exec.variables

    def ensure_not_cancelled(self) -> None:
        self.exec.ensure_not_cancelled()

    def service(self, name: str) -> Any:
        try:
            return self.exec.services[name]
        except KeyError:
            raise RuntimeError(f"service {name!r} is not configured") from None
