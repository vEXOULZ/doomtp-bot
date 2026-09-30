"""The logs.ivr.fi provider against a local stand-in (ADR-0008): the request, 404s, and its own limits."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from aiohttp import web

from doomtp_bot.history.provider import NOT_LOGGED, PAUSED, IvrLogsProvider

LINE = "@id=a;user-id=400;tmi-sent-ts=1000 :alice!alice@x PRIVMSG #doomtp :hi"


@dataclass
class Stand:
    """What the stand-in answers, and what it was asked."""

    url: str = ""
    logs_status: int = 200
    logs_body: str = LINE + "\n"
    channels_status: int = 200
    asked: list[tuple[str, dict[str, str]]] = field(default_factory=list)


@pytest.fixture
async def stand() -> AsyncIterator[Stand]:
    state = Stand()

    async def channels(request: web.Request) -> web.Response:
        state.asked.append((request.path, dict(request.query)))
        if state.channels_status != 200:
            return web.Response(status=state.channels_status)
        return web.json_response({"channels": [{"name": "doomtp", "userID": "100"}]})

    async def logs(request: web.Request) -> web.Response:
        state.asked.append((request.path, dict(request.query)))
        return web.Response(status=state.logs_status, text=state.logs_body)

    app = web.Application()
    app.router.add_get("/channels", channels)
    app.router.add_get("/channelid/{id}", logs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    yield state
    await runner.cleanup()


def provider_for(stand: Stand, **limits: object) -> IvrLogsProvider:
    kwargs: dict[str, object] = {"min_interval_s": 0.0, "backoff_s": (0.0, 0.0)} | limits
    return IvrLogsProvider(stand.url, **kwargs)  # type: ignore[arg-type]


async def test_a_range_is_asked_for_by_channel_id_as_raw_lines(stand: Stand) -> None:
    provider = provider_for(stand)
    response = await provider.fetch(
        "100", from_ms=1_772_074_800_000, to_ms=1_772_161_200_001, limit=2, offset=4
    )
    await provider.close()
    assert response.ok and response.lines == (LINE,) and not response.hit_limit
    assert stand.asked == [
        ("/channels", {}),
        (
            "/channelid/100",
            {
                "from": "2026-02-26T03:00:00.000Z",
                "to": "2026-02-27T03:00:00.001Z",
                "raw": "true",
                "limit": "2",
                "offset": "4",
            },
        ),
    ]


async def test_a_404_is_a_range_with_no_chat(stand: Stand) -> None:
    stand.logs_status, stand.logs_body = 404, "Not found"
    provider = provider_for(stand)
    response = await provider.fetch("100", from_ms=0, to_ms=1000, limit=10)
    await provider.close()
    assert response.ok and response.lines == ()


async def test_a_channel_the_service_does_not_log_is_an_error_not_an_empty_range(stand: Stand) -> None:
    provider = provider_for(stand)
    response = await provider.fetch("999", from_ms=0, to_ms=1000, limit=10)
    await provider.close()
    assert response.error_code == NOT_LOGGED
    assert [path for path, _ in stand.asked] == ["/channels"]  # the logs weren't asked for


async def test_the_channel_list_is_asked_for_once(stand: Stand) -> None:
    provider = provider_for(stand)
    await provider.fetch("100", from_ms=0, to_ms=1000, limit=10)
    await provider.fetch("100", from_ms=1000, to_ms=2000, limit=10)
    await provider.close()
    assert [path for path, _ in stand.asked] == ["/channels", "/channelid/100", "/channelid/100"]


async def test_failing_three_times_in_a_row_pauses_until_the_next_day(stand: Stand) -> None:
    stand.logs_status = 503
    provider = provider_for(stand)
    response = await provider.fetch("100", from_ms=0, to_ms=1000, limit=10)
    assert response.error_code == PAUSED and response.retry_at_ms is not None
    assert response.retry_at_ms % 86_400_000 == 0  # midnight UTC
    assert [path for path, _ in stand.asked] == ["/channels"] + ["/channelid/100"] * 3

    stand.logs_status = 200
    again = await provider.fetch("100", from_ms=0, to_ms=1000, limit=10)
    await provider.close()
    assert again.error_code == PAUSED
    assert len(stand.asked) == 4  # nothing asked while paused


async def test_a_failing_channel_list_is_retried_and_pauses(stand: Stand) -> None:
    stand.channels_status = 429
    provider = provider_for(stand)
    response = await provider.fetch("100", from_ms=0, to_ms=1000, limit=10)
    await provider.close()
    assert response.error_code == PAUSED
    assert [path for path, _ in stand.asked] == ["/channels"] * 3


async def test_the_daily_budget_pauses_the_provider(stand: Stand) -> None:
    provider = provider_for(stand, daily_budget=2)
    first = await provider.fetch("100", from_ms=0, to_ms=1000, limit=10)  # /channels and the logs
    second = await provider.fetch("100", from_ms=0, to_ms=1000, limit=10)
    await provider.close()
    assert first.ok
    assert second.error_code == PAUSED and second.retry_at_ms is not None
    assert len(stand.asked) == 2


async def test_another_answer_is_an_error_for_that_request(stand: Stand) -> None:
    stand.logs_status = 400
    provider = provider_for(stand)
    response = await provider.fetch("100", from_ms=0, to_ms=1000, limit=10)
    await provider.close()
    assert response.error_code == "http_400"


async def test_a_service_that_cannot_be_reached_pauses(stand: Stand) -> None:
    provider = IvrLogsProvider("http://127.0.0.1:9", min_interval_s=0.0, backoff_s=(0.0, 0.0))
    response = await provider.fetch("100", from_ms=0, to_ms=1000, limit=10)
    await provider.close()
    assert response.error_code == PAUSED
