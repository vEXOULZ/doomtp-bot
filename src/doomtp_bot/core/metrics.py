"""Counters, exported as Prometheus text on `/metrics` (ADR-0015, architecture §13).

Each counter is incremented where the thing happens and lives in memory: a restart starts them from zero,
which scrapers read as a reset. Labels are small fixed sets — never a channel or a user — so the output
can't grow with the audience.
"""

from __future__ import annotations

from collections.abc import Iterator

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class Counter:
    def __init__(
        self,
        name: str,
        help_text: str,
        labels: tuple[str, ...] = (),
        *,
        registry: list[Counter] | None = None,
    ) -> None:
        self.name = name
        self.help = help_text
        self.labels = labels
        self._values: dict[tuple[str, ...], float] = {}
        (REGISTRY if registry is None else registry).append(self)

    def inc(self, amount: float = 1, **labels: object) -> None:
        if amount <= 0:
            return  # a counter only goes up; zero would only add an empty series
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0) + amount

    def value(self, **labels: object) -> float:
        return self._values.get(self._key(labels), 0)

    def _key(self, labels: dict[str, object]) -> tuple[str, ...]:
        if set(labels) != set(self.labels):
            raise ValueError(f"{self.name} takes labels {self.labels}, got {tuple(labels)}")
        return tuple(str(labels[name]) for name in self.labels)

    def render(self) -> Iterator[str]:
        yield f"# HELP {self.name} {_escape_help(self.help)}"
        yield f"# TYPE {self.name} counter"
        if not self.labels:
            yield f"{self.name} {_number(self._values.get((), 0))}"
            return
        for key, value in sorted(self._values.items()):
            pairs = ",".join(f'{name}="{_escape_label(v)}"' for name, v in zip(self.labels, key, strict=True))
            yield f"{self.name}{{{pairs}}} {_number(value)}"


REGISTRY: list[Counter] = []


def render(registry: list[Counter] | None = None) -> str:
    counters = REGISTRY if registry is None else registry
    return "".join(f"{line}\n" for counter in counters for line in counter.render())


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(value)


# ── the counters (ADR-0015 lists where each is incremented) ─────────────────
MESSAGES_LOGGED = Counter(
    "messages_logged_total", "Chat messages stored in the log, by where they came from.", ("source",)
)
BACKFILL_INSERTED = Counter(
    "backfill_inserted_total", "Messages and moderation events recovered from the history service."
)
BACKFILL_INCOMPLETE = Counter(
    "backfill_incomplete_total", "Backfill runs that could not cover their whole gap."
)
RUNS = Counter("runs_total", "Command runs finished, by exit code.", ("code",))
RUNS_CANCELLED = Counter("runs_cancelled_total", "Runs whose writes were discarded, by why.", ("reason",))
COOLDOWN_REJECTIONS = Counter(
    "cooldown_rejections_total", "Invocations refused because a cooldown was running, by tier.", ("tier",)
)
FILTER_HITS = Counter(
    "filter_hits_total", "Badword filter matches in what the bot sends, by action.", ("action",)
)
OUTBOX_DROPPED = Counter("outbox_dropped_total", "Outgoing messages not sent, by reason.", ("reason",))
EVENTSUB_WELCOMES = Counter(
    "eventsub_welcomes_total",
    "EventSub sessions welcomed: one per token at startup, and one more for every reconnect.",
)
TWITCH_CLIENT_RESTARTS = Counter(
    "twitch_client_restarts_total", "Times the Twitch client stopped on its own and was started again."
)
