"""The bot's own events (ADR-0034): what it records about each run, and which triggers that fires."""

from __future__ import annotations

from typing import Any

from doomtp_bot.core.bot_events import BotEvents, BotRuns, LastRun
from doomtp_bot.core.build import BuildInfo
from doomtp_bot.storage.db import Databases, fetch_one

OLD = BuildInfo(version="0.12.0", release="v0.12.0", commit="a" * 40)
NEW = BuildInfo(version="0.13.0", release="v0.13.0", commit="b" * 40)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000

    def __call__(self) -> int:
        return self.now


async def test_each_start_is_recorded_and_the_next_one_reads_it(dbs: Databases) -> None:
    clock = Clock()
    first = BotRuns(dbs.chatlog, OLD, clock)
    assert await first.begin() is None
    clock.now += 120_000
    await first.beat()
    clock.now += 60_000
    await first.end()
    row = await fetch_one(dbs.chatlog, "SELECT * FROM bot_runs WHERE id = %s", (first.id,))
    assert row is not None and (row["version"], row["release"], row["ended_at"]) == ("0.12.0", "v0.12.0", clock.now)

    clock.now += 300_000
    second = BotRuns(dbs.chatlog, NEW, clock)
    assert await second.begin() == LastRun("0.12.0", 1_180_000)


class Fired:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, channel_id: str, type_: str, payload: dict[str, Any]) -> None:
        self.calls.append((channel_id, type_, payload))


async def test_the_first_time_online_says_why_and_an_update_comes_first() -> None:
    fired = Fired()
    events = BotEvents(fired, lambda: ["100", "200"], NEW)
    await events.online(LastRun("0.12.0", 1_000_000), now=1_042_000)
    assert [(c, t) for c, t, _ in fired.calls] == [
        ("100", "bot_updated"),
        ("100", "bot_online"),
        ("200", "bot_updated"),
        ("200", "bot_online"),
    ]
    assert fired.calls[0][2] == {"version": "0.13.0", "previous": "0.12.0"}
    assert fired.calls[1][2] == {"reason": "update", "down_s": 42, "version": "0.13.0"}

    fired.calls.clear()
    await events.online(None, down_since_ms=2_000_000, now=2_010_000)  # Twitch dropped it and it came back
    assert fired.calls[0][1:] == (
        "bot_online",
        {"reason": "reconnect", "down_s": 10, "version": "0.13.0", "cause": "unknown"},
    )


async def test_a_first_run_or_the_same_version_is_a_plain_start() -> None:
    fired = Fired()
    await BotEvents(fired, lambda: ["100"], NEW).online(None)
    await BotEvents(fired, lambda: ["100"], NEW).online(LastRun("0.13.0", 1_000), now=61_000)
    assert [p for _, _, p in fired.calls] == [
        {"reason": "start", "down_s": None, "version": "0.13.0"},
        {"reason": "start", "down_s": 60, "version": "0.13.0"},
    ]


async def test_one_channels_failure_does_not_stop_the_others() -> None:
    seen: list[str] = []

    async def fire(channel_id: str, type_: str, payload: dict[str, Any]) -> None:
        seen.append(channel_id)
        if channel_id == "100":
            raise RuntimeError("boom")

    await BotEvents(fire, lambda: ["100", "200"], NEW).joined("100")
    await BotEvents(fire, lambda: ["100", "200"], NEW).online(None)
    assert seen == ["100", "100", "200"]


async def test_a_reconnect_says_why_the_connection_was_restarted() -> None:
    fired = Fired()
    events = BotEvents(fired, lambda: ["100"], NEW)
    await events.online(None)
    await events.online(None, down_since_ms=2_000_000, cause="deaf: no EventSub connection carries chat", now=2_010_000)
    assert fired.calls[1][2]["cause"] == "deaf: no EventSub connection carries chat"
    await events.online(None, down_since_ms=2_000_000, now=2_010_000)
    assert fired.calls[2][2]["cause"] == "unknown"
    assert "cause" not in fired.calls[0][2]
