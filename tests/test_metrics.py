"""Counters on /metrics (ADR-0015): the text format, the endpoint, and one increment at each source.

The counters are process-wide, so every test reads a difference rather than an absolute value.
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import httpx
import pytest

from doomtp_bot.api.app import create_app
from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.core import metrics
from doomtp_bot.core.events import ChatMessage
from doomtp_bot.core.health import HealthRegistry
from doomtp_bot.core.outbox import Outbox, SendResult
from doomtp_bot.filters.service import FilterService
from doomtp_bot.storage.db import Databases
from doomtp_bot.twitch.client import TwitchService, _BotClient
from tests.runtime.helpers import make_runtime, run

CHANNEL = "100"


def test_counters_render_as_prometheus_text() -> None:
    registry: list[metrics.Counter] = []
    plain = metrics.Counter("things_total", "Things.\nCounted.", registry=registry)
    labelled = metrics.Counter("drops_total", "Drops, by why.", ("reason",), registry=registry)
    labelled.inc(reason='say "no"\\')
    labelled.inc(2, reason="ttl")
    labelled.inc(0, reason="never")  # nothing, not an empty series

    assert metrics.render(registry).splitlines() == [
        "# HELP things_total Things.\\nCounted.",
        "# TYPE things_total counter",
        "things_total 0",
        "# HELP drops_total Drops, by why.",
        "# TYPE drops_total counter",
        'drops_total{reason="say \\"no\\"\\\\"} 1',
        'drops_total{reason="ttl"} 2',
    ]
    assert plain.value() == 0 and labelled.value(reason="ttl") == 2


@pytest.mark.parametrize("wrong", [{}, {"reason": "a", "channel": "100"}, {"channel": "100"}])
def test_a_counter_refuses_labels_it_does_not_have(wrong: dict[str, str]) -> None:
    counter = metrics.Counter("x_total", "x", ("reason",), registry=[])
    with pytest.raises(ValueError, match="takes labels"):
        counter.inc(**wrong)


async def test_metrics_are_served_beside_the_health_checks() -> None:
    transport = httpx.ASGITransport(app=create_app(HealthRegistry()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    for name in (
        "messages_logged_total", "backfill_inserted_total", "backfill_incomplete_total", "runs_total",
        "runs_cancelled_total", "cooldown_rejections_total", "filter_hits_total", "outbox_dropped_total",
        "eventsub_welcomes_total", "twitch_client_restarts_total",
    ):  # fmt: skip
        assert f"# TYPE {name} counter" in response.text


async def test_the_log_counts_rows_it_inserted_not_rows_it_was_offered(dbs: Databases) -> None:
    before = metrics.MESSAGES_LOGGED.value(source="eventsub")
    writer = ChatLogWriter(dbs.chatlog, flush_interval=0.01)
    writer.start()
    message = ChatMessage(
        message_id="m1", channel_id=CHANNEL, channel_login="doomtp", user_id="400", user_login="alice",
        display_name="Alice", text="hi", sent_at=1, received_at=1,
    )  # fmt: skip
    await writer.message(message)
    await writer.message(message)  # a redelivery: ON CONFLICT DO NOTHING, so not a message logged
    await writer.stop()
    assert metrics.MESSAGES_LOGGED.value(source="eventsub") - before == 1


async def test_runs_are_counted_by_code_and_cancellations_by_reason() -> None:
    ok, cancelled = metrics.RUNS.value(code=0), metrics.RUNS_CANCELLED.value(reason="moderated")
    timed_out = metrics.RUNS_CANCELLED.value(reason="timeout")
    await run(make_runtime(), "!echo hi")
    await run(make_runtime(), "!cancelme", is_cancelled=lambda: True)
    await run(make_runtime(expr_timeout=0.05), "!slow")
    assert metrics.RUNS.value(code=0) - ok == 1
    assert metrics.RUNS_CANCELLED.value(reason="moderated") - cancelled == 1
    assert metrics.RUNS_CANCELLED.value(reason="timeout") - timed_out == 1


async def test_filter_hits_and_outbox_drops_are_counted(dbs: Databases) -> None:
    masked, blocked = metrics.FILTER_HITS.value(action="mask"), metrics.FILTER_HITS.value(action="block")
    dropped = metrics.OUTBOX_DROPPED.value(reason="filter_block")
    filters = FilterService(dbs.bot)
    await filters.reload()
    await filters.add(channel_id=CHANNEL, pattern="bad", actor_user_id="300", via="chat")
    await filters.add(channel_id=CHANNEL, pattern="worse", action="block", actor_user_id="300", via="chat")

    class Sender:
        async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
            return SendResult("t1")

    outbox = Outbox(Sender(), content_filter=filters.apply)
    await outbox.send(CHANNEL, "bad and bad")
    await outbox.send(CHANNEL, "worse")
    assert metrics.FILTER_HITS.value(action="mask") - masked == 2
    assert metrics.FILTER_HITS.value(action="block") - blocked == 1
    assert metrics.OUTBOX_DROPPED.value(reason="filter_block") - dropped == 1


async def test_eventsub_welcomes_and_client_restarts_are_counted() -> None:
    welcomes, restarts = metrics.EVENTSUB_WELCOMES.value(), metrics.TWITCH_CLIENT_RESTARTS.value()
    await _BotClient.event_websocket_welcome(None, NS(id="session-1"))  # type: ignore[arg-type]

    async def again() -> None:
        return None

    async def start(**kwargs: Any) -> None:
        return None  # returned on its own: EventSub gave up

    service = TwitchService(client_id="x", client_secret="y", tokens=None, sink=None, on_stopped=again)  # type: ignore[arg-type]
    client = NS(start=start)
    service.client = client  # type: ignore[assignment]
    await service._run(client)  # type: ignore[arg-type]
    assert metrics.EVENTSUB_WELCOMES.value() - welcomes == 1
    assert metrics.TWITCH_CLIENT_RESTARTS.value() - restarts == 1
