"""Backfill as queued jobs (ADR-0024 §5): queueing, consent, one worker, cancelling, restarts."""

from __future__ import annotations

import asyncio

import pytest

from doomtp_bot.history.backfill import OUT_OF_REACH
from doomtp_bot.history.provider import HistoryResponse
from doomtp_bot.history.queue import GAPS, STARTUP, BackfillQueue, BackfillRefused
from doomtp_bot.policy.repository import Actor
from doomtp_bot.storage.db import Databases
from tests.test_history import CHANNEL_ID, CHANNEL_LOGIN, PRIVMSG, FakeProvider, backfill_for, privmsg

SESSIONS = """
INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason) VALUES ('100', 0, 1100, 'shutdown');
INSERT INTO log_sessions (channel_id, started_at, ended_at, end_reason) VALUES ('100', 60000, 70000, 'crash');
INSERT INTO log_sessions (channel_id, started_at) VALUES ('100', 90000);
"""


async def queue_for(dbs: Databases, provider: FakeProvider, *, opted_in: bool = True) -> BackfillQueue:
    return BackfillQueue(await backfill_for(dbs, provider, opted_in=opted_in))


async def test_a_range_is_queued_once_run_and_recorded(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    queue = await queue_for(dbs, provider)

    job = await queue.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
    assert job is not None and (job.state, job.requested_by) == ("queued", "chat:1")
    assert await queue.queue_range(CHANNEL_ID, 1100, 6000, "startup") is None  # already queued

    (done,) = await queue.drain()
    await queue.service.writer.stop()
    assert (done.id, done.state, done.fetched, done.inserted, done.complete) == (job.id, "done", 1, 1, True)
    assert provider.calls == [(CHANNEL_LOGIN, 0, 6001, 800)]
    assert await queue.run_next() is None
    # Finished, the same range can be asked for again.
    assert await queue.queue_range(CHANNEL_ID, 1100, 6000, "chat:1") is not None


async def test_a_channel_with_backfill_off_queues_nothing(dbs: Databases) -> None:
    queue = await queue_for(dbs, FakeProvider(), opted_in=False)
    with pytest.raises(BackfillRefused):
        await queue.queue_range(CHANNEL_ID, 0, 1000, "chat:1")
    with pytest.raises(BackfillRefused):
        await queue.queue_gaps(CHANNEL_ID, "chat:1")
    assert await queue.queue_startup() == []


@pytest.mark.parametrize(("from_ms", "to_ms"), [(5000, 5000), (5000, 4000), (-1, 10)])
async def test_a_range_that_makes_no_sense_is_refused(dbs: Databases, from_ms: int, to_ms: int) -> None:
    queue = await queue_for(dbs, FakeProvider())
    with pytest.raises(BackfillRefused):
        await queue.queue_range(CHANNEL_ID, from_ms, to_ms, "chat:1")


async def test_turning_backfill_off_cancels_what_was_waiting(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    queue = await queue_for(dbs, provider)
    await queue.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
    await queue.service.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "history_backfill", False, Actor(None, "s"))
    )
    (job,) = await queue.drain()
    assert (job.state, job.error) == ("cancelled", "backfill is off for this channel")
    assert provider.calls == []


async def test_startup_queues_one_job_for_the_gaps_and_the_job_a_stop_cut_short(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    queue = await queue_for(dbs, provider)
    await dbs.chatlog.execute(SESSIONS)
    cut_short = await queue.queue_range(CHANNEL_ID, 100, 200, "chat:1")
    assert cut_short is not None
    await dbs.chatlog.execute("UPDATE backfill_jobs SET state = 'running', started_at = 1")

    (gaps,) = await queue.queue_startup()
    assert (gaps.kind, gaps.from_ms, gaps.to_ms, gaps.requested_by) == (GAPS, 1100, 90000, STARTUP)
    jobs = await queue.jobs(CHANNEL_ID)
    assert [(j.id, j.state) for j in jobs] == [(cut_short.id, "queued"), (gaps.id, "queued")]
    # A reconnect, or the broadcaster asking, joins the job that is waiting.
    assert await queue.queue_startup() == []
    assert await queue.queue_gaps(CHANNEL_ID, "chat:1") == (gaps, False)
    await queue.drain()
    await queue.service.writer.stop()
    # Both gaps from one request: history from 1005 reaches back past the first one.
    assert provider.calls == [
        (CHANNEL_LOGIN, 0, 201, 800),
        (CHANNEL_LOGIN, 0, 90001, 800),
    ]  # the range, the gaps
    assert await queue.queue_startup() == []  # the gaps are filled now
    assert await queue.queue_gaps(CHANNEL_ID, "chat:1") == (None, False)


async def test_a_gap_older_than_the_history_is_not_queued_again(dbs: Databases) -> None:
    # History starts at 64000: the first gap (1100-60000) can't be reached, the second (70000-90000) can.
    provider = FakeProvider(HistoryResponse(lines=(privmsg(64000, "a"), privmsg(80000, "b"))))
    queue = await queue_for(dbs, provider)
    await dbs.chatlog.execute(SESSIONS)
    await queue.queue_startup()
    (job,) = await queue.drain()
    await queue.service.writer.stop()
    assert (job.state, job.fetched, job.inserted, job.complete, job.error) == ("done", 2, 1, False, None)
    async with await dbs.chatlog.execute(
        "SELECT gap_from, complete, error FROM backfill_runs ORDER BY gap_from"
    ) as cur:
        assert [tuple(r.values()) for r in await cur.fetchall()] == [
            (1100, False, OUT_OF_REACH),
            (70000, True, None),
        ]
    assert await queue.queue_startup() == []  # asking again can't reach further back


async def test_only_a_queued_job_can_be_cancelled(dbs: Databases) -> None:
    queue = await queue_for(dbs, FakeProvider(HistoryResponse(lines=(PRIVMSG,))))
    first = await queue.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
    second = await queue.queue_range(CHANNEL_ID, 7000, 8000, "chat:1")
    assert first is not None and second is not None

    cancelled = await queue.cancel(CHANNEL_ID, second.id)
    assert cancelled is not None and cancelled.state == "cancelled"
    assert await queue.cancel(CHANNEL_ID, second.id) is None  # not queued any more
    assert await queue.cancel("elsewhere", first.id) is None  # another channel's id
    assert [j.id for j in await queue.drain()] == [first.id]
    await queue.service.writer.stop()
    # Finished jobs come after the open ones, newest first.
    assert [j.id for j in await queue.jobs(CHANNEL_ID)] == [second.id, first.id]


async def test_a_failing_provider_fails_the_job_and_the_next_one_still_runs(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(error_code="channel_not_joined"))
    queue = await queue_for(dbs, provider)
    await queue.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
    await queue.queue_range(CHANNEL_ID, 7000, 8000, "chat:1")
    jobs = await queue.drain()
    assert [(j.state, j.error) for j in jobs] == [("failed", "channel_not_joined")] * 2


async def test_the_worker_runs_what_is_queued_while_it_waits(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    queue = await queue_for(dbs, provider)
    queue.start()
    try:
        await queue.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
        for _ in range(200):
            (job,) = await queue.jobs(CHANNEL_ID)
            if job.state == "done":
                break
            await asyncio.sleep(0.01)
        assert job.state == "done"
    finally:
        await queue.stop()
        await queue.service.writer.stop()


class HeldProvider(FakeProvider):
    """Answers only once the test lets it, so a stop can arrive while a job is running."""

    def __init__(self, response: HistoryResponse) -> None:
        super().__init__(response)
        self.asked = asyncio.Event()
        self.answer = asyncio.Event()

    async def fetch(
        self,
        channel_login: str,
        *,
        after_ms: int | None = None,
        before_ms: int | None = None,
        limit: int = 800,
    ) -> HistoryResponse:
        self.asked.set()
        await self.answer.wait()
        return await super().fetch(channel_login, after_ms=after_ms, before_ms=before_ms, limit=limit)


async def test_stopping_lets_the_running_job_finish(dbs: Databases) -> None:
    # A cancel could land inside psycopg's savepoint bookkeeping and break the shared connection.
    provider = HeldProvider(HistoryResponse(lines=(PRIVMSG,)))
    queue = await queue_for(dbs, provider)
    queue.start()
    await queue.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
    await asyncio.wait_for(provider.asked.wait(), 5)

    stopping = asyncio.create_task(queue.stop())
    await asyncio.sleep(0)
    assert not stopping.done()  # waits for the job rather than cutting it short
    provider.answer.set()
    await asyncio.wait_for(stopping, 5)
    await queue.service.writer.stop()

    (job,) = await queue.jobs(CHANNEL_ID)
    assert job.state == "done"
    # The connection is still usable, and the fixture's rollback will be at the right nesting level.
    assert await queue.queue_range(CHANNEL_ID, 7000, 8000, "chat:1") is not None
