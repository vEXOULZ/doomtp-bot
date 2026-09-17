"""Component health registry backing /healthz and /readyz (architecture §11)."""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class Status(enum.StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"  # component intentionally not running (e.g. Twitch not configured)


@dataclass(frozen=True)
class ComponentHealth:
    status: Status
    detail: dict[str, Any] = field(default_factory=dict)


HealthCheck = Callable[[], Awaitable[ComponentHealth]]


class HealthRegistry:
    def __init__(self) -> None:
        self._checks: dict[str, HealthCheck] = {}

    def register(self, name: str, check: HealthCheck) -> None:
        self._checks[name] = check

    async def snapshot(self) -> tuple[Status, dict[str, ComponentHealth]]:
        results: dict[str, ComponentHealth] = {}
        for name, check in self._checks.items():
            try:
                results[name] = await check()
            except Exception as exc:  # a failing check must not break the endpoint
                results[name] = ComponentHealth(Status.UNHEALTHY, {"error": repr(exc)})
        statuses = {r.status for r in results.values()}
        if Status.UNHEALTHY in statuses:
            overall = Status.UNHEALTHY
        elif Status.DEGRADED in statuses:
            overall = Status.DEGRADED
        else:
            overall = Status.OK
        return overall, results
