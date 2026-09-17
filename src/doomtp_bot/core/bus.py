"""Tiny in-process async pub/sub (ADR-0004). Subscribers run in registration order; one failing handler
does not stop the others."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import structlog

log = structlog.get_logger(__name__)

E = TypeVar("E")
Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[Any], list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type[E], handler: Callable[[E], Awaitable[None]]) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: object) -> None:
        for handler in tuple(self._handlers.get(type(event), ())):
            try:
                await handler(event)
            except Exception:
                log.exception("bus.handler_failed", event=type(event).__name__, handler=repr(handler))
