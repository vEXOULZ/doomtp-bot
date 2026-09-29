"""The starter readouts, `:human` and `$channel.next_stream` (ADR-0019 item 6)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from doomtp_bot import __version__
from doomtp_bot.core.schedule import FAILED_TTL_S, NextStreams
from doomtp_bot.customcmds.resolution import SystemResolver
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.runtime.result import ErrorCode
from doomtp_bot.runtime.variables import InMemoryVariableStore, VarKey
from scripts.starter_pack import STARTER
from tests.runtime.helpers import CHANNEL, registry, run, with_core

NOW = 1_800_000_000.0  # 2027-01-15 08:00 UTC
LIVE = replace(CHANNEL, live=True, title="any% practice", game="DOOM", viewers=42, started_at=NOW - 3725)


class FakeSchedule:
    def __init__(self, *segments: dict[str, str], fails: bool = False) -> None:
        self.segments = list(segments)
        self.fails = fails
        self.asked = 0

    async def fetch_schedule(self, channel_id: str) -> list[dict[str, str]]:
        self.asked += 1
        if self.fails:
            raise RuntimeError("helix is down")
        return self.segments


def readouts(store: InMemoryVariableStore | None = None, **services: Any) -> Runtime:
    reg = registry()
    resolver = SystemResolver.from_derived(with_core(reg), "starter", STARTER)
    return Runtime(reg, resolver=resolver, store=store or InMemoryVariableStore(), services=services)


async def said(runtime: Runtime, text: str, **kwargs: Any) -> str | None:
    report = await run(runtime, text, clock=lambda: NOW, **kwargs)
    assert report.result.code == 0, report.result
    return report.send


@pytest.mark.parametrize(
    ("text", "channel", "expected"),
    [
        ("!ping", CHANNEL, "pong"),
        ("!uptime", LIVE, "DoomTP has been live for 1h 2m"),
        ("!uptime", CHANNEL, "DoomTP isn't live right now"),
        ("!title", LIVE, "any% practice"),
        ("!title", CHANNEL, "no title, the stream is offline"),
        ("!game", LIVE, "DoomTP is playing DOOM"),
        ("!game", CHANNEL, "DoomTP is playing nothing right now"),
        ("!viewers", LIVE, "42 watching"),
        ("!time", CHANNEL, "it's 08:00 on Friday here"),
        ("!bot", CHANNEL, f"I'm doomtp-bot v{__version__}"),
        ("!nextstream", CHANNEL, "nothing on DoomTP's schedule right now"),
    ],
)
async def test_each_readout_says_its_own_wording(text: str, channel: Any, expected: str) -> None:
    assert await said(readouts(), text, channel=channel) == expected


async def test_a_readout_takes_the_channels_wording_when_it_has_one() -> None:
    store = InMemoryVariableStore()
    store.data[VarKey("channel", "c1", name="customecho")] = {
        "uptime": "{$channel.display} has been at it for {$channel.uptime:human}, send snacks"
    }
    runtime = readouts(store)
    assert await said(runtime, "!uptime", channel=LIVE) == "DoomTP has been at it for 1h 2m, send snacks"
    assert await said(runtime, "!uptime", channel=CHANNEL) == "DoomTP isn't live right now"


async def test_nextstream_reads_the_first_stream_still_to_come() -> None:
    schedule = FakeSchedule(
        {"title": "", "category": "DOOM", "start": "2027-01-15T07:00:00+00:00"},  # already started
        {"title": "", "category": "DOOM", "start": "2027-01-16T10:30:00+00:00"},
        {"title": "later", "category": "", "start": "2027-01-20T10:30:00+00:00"},
    )
    runtime = readouts(schedule=NextStreams(schedule))
    assert await said(runtime, "!nextstream") == "next stream in 1d 2h: untitled (DOOM)"
    assert await said(runtime, "!echo {$channel.next_stream[start]}") == "2027-01-16T10:30:00+00:00"
    assert await said(runtime, "!echo {$channel.next_stream[in]}") == str(26 * 3600 + 1800)
    assert schedule.asked == 1  # the second and third reads came from the cache


async def test_a_failed_schedule_request_reads_as_nothing_scheduled_for_a_while() -> None:
    now = [0.0]
    schedule = FakeSchedule(fails=True)
    streams = NextStreams(schedule, clock=lambda: now[0])
    assert await streams.next_stream("c1", NOW) is None
    schedule.fails = False
    schedule.segments = [{"title": "t", "category": "c", "start": "2027-01-16T08:00:00+00:00"}]
    assert await streams.next_stream("c1", NOW) is None  # the failure is remembered briefly
    now[0] = FAILED_TTL_S + 1
    assert await streams.next_stream("c1", NOW) == {
        "title": "t",
        "category": "c",
        "start": "2027-01-16T08:00:00+00:00",
        "in": 86400,
    }
    assert schedule.asked == 2


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (45, "45s"),
        (125, "2m 5s"),
        (3600, "1h"),
        (3725, "1h 2m"),
        (90061, "1d 1h"),
        ("61", "1m 1s"),
    ],
)
async def test_human_says_seconds_in_its_two_largest_units(seconds: Any, text: str) -> None:
    runtime = readouts()
    assert await said(runtime, f"!echo {{{seconds!r}:human}}".replace("'", '"')) == text


@pytest.mark.parametrize("value", ['"soon"', "true", "(0 - 5)"])
async def test_human_needs_a_number_of_seconds(value: str) -> None:
    report = await run(readouts(), f"!echo {{{value}:human}}")
    assert report.result.code == ErrorCode.E_TYPE
