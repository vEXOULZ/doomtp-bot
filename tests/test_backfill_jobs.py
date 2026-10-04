"""Backfill as vex-platform's `chat_backfill` job kind (ADR-0027): queueing, consent, pausing, cancelling,
and the jobs an older image left queued."""

from __future__ import annotations

import asyncio

import pytest
from vex_platform.actor import Actor as JobActor

from doomtp_bot.audit.log import TABLE as AUDIT_TABLE
from doomtp_bot.clock import now_ms
from doomtp_bot.core import metrics
from doomtp_bot.history.backfill import STOPPED, Gap
from doomtp_bot.history.jobs import GAPS, OFF, STARTUP, BackfillJob, BackfillJobs, BackfillRefused
from doomtp_bot.history.provider import PAUSED, HistoryResponse
from doomtp_bot.history.queue import BackfillQueue
from doomtp_bot.policy.repository import Actor
from doomtp_bot.storage.db import Databases
from tests.conftest import MakeBackfillJobs
from tests.test_backfill_queue import SESSIONS, HeldProvider
from tests.test_history import CHANNEL_ID, CHANNEL_LOGIN, PRIVMSG, FakeProvider, backfill_for, privmsg

BROADCASTER = JobActor("user", CHANNEL_ID, CHANNEL_LOGIN, "chat")


async def until(jobs: BackfillJobs, job_id: int, *states: str) -> BackfillJob:
    """The job once it is in one of `states`, as the worker gets there."""
    for _ in range(500):
        job = BackfillJob.of_run(await jobs.runtime.get(job_id))
        if job.state in states:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} is {job.state}, not {states}")


async def counted(event: str, before: float) -> float:
    """How many more `event`s `backfill_jobs_total` has than `before`; the hook runs just after the state
    is written, so it waits a moment for it."""
    for _ in range(100):
        if metrics.BACKFILL_JOBS.value(event=event) > before:
            break
        await asyncio.sleep(0.01)
    return metrics.BACKFILL_JOBS.value(event=event) - before


async def test_a_range_is_queued_once_run_and_recorded(dbs: Databases, make_backfill_jobs: MakeBackfillJobs) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    jobs = await make_backfill_jobs(await backfill_for(dbs, provider))
    started, succeeded = (metrics.BACKFILL_JOBS.value(event=e) for e in ("started", "succeeded"))

    job = await jobs.queue_range(CHANNEL_ID, 1100, 6000, "chat:1", actor=BROADCASTER)
    assert job is not None and (job.state, job.requested_by, job.kind) == ("queued", "chat:1", "range")
    assert await jobs.queue_range(CHANNEL_ID, 1100, 6000, "startup") is None  # already queued

    await jobs.runtime.start()
    done = await until(jobs, job.id, "done", "failed")
    await jobs.service.writer.stop()
    assert (done.state, done.fetched, done.inserted, done.complete) == ("done", 1, 1, True)
    assert done.started_at is not None and done.finished_at is not None
    assert provider.calls == [(CHANNEL_ID, 0, 6001, 1000, 0)]
    assert (await jobs.runtime.get(job.id)).actor == BROADCASTER
    assert (await counted("started", started), await counted("succeeded", succeeded)) == (1, 1)
    # Finished, the same range can be asked for again.
    assert await jobs.queue_range(CHANNEL_ID, 1100, 6000, "chat:1") is not None


async def test_a_channel_with_backfill_off_queues_nothing(dbs: Databases, make_backfill_jobs: MakeBackfillJobs) -> None:
    jobs = await make_backfill_jobs(await backfill_for(dbs, FakeProvider(), opted_in=False))
    with pytest.raises(BackfillRefused):
        await jobs.queue_range(CHANNEL_ID, 0, 1000, "chat:1")
    with pytest.raises(BackfillRefused):
        await jobs.queue_gaps(CHANNEL_ID, "chat:1")
    assert await jobs.queue_startup() == []


@pytest.mark.parametrize(("from_ms", "to_ms"), [(5000, 5000), (5000, 4000), (-1, 10)])
async def test_a_range_that_makes_no_sense_is_refused(
    dbs: Databases, make_backfill_jobs: MakeBackfillJobs, from_ms: int, to_ms: int
) -> None:
    jobs = await make_backfill_jobs(await backfill_for(dbs, FakeProvider()))
    with pytest.raises(BackfillRefused):
        await jobs.queue_range(CHANNEL_ID, from_ms, to_ms, "chat:1")


async def test_turning_backfill_off_cancels_what_was_waiting(
    dbs: Databases, make_backfill_jobs: MakeBackfillJobs
) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    jobs = await make_backfill_jobs(await backfill_for(dbs, provider))
    job = await jobs.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
    assert job is not None
    await jobs.service.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "history_backfill", False, Actor(None, "s"))
    )
    await jobs.runtime.start()
    cancelled = await until(jobs, job.id, "cancelled", "done", "failed")
    assert (cancelled.state, cancelled.error) == ("cancelled", OFF)
    assert provider.calls == []
    async with await dbs.chatlog.execute(
        f"SELECT action, actor_kind FROM {AUDIT_TABLE} WHERE job_run_id = %s ORDER BY id", (job.id,)
    ) as cur:
        assert [tuple(r.values()) for r in await cur.fetchall()] == [
            ("job.enqueue", "system"),
            ("job.cancel", "system"),
        ]


async def test_startup_queues_one_job_for_the_gaps_that_later_requests_join(
    dbs: Databases, make_backfill_jobs: MakeBackfillJobs
) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    jobs = await make_backfill_jobs(await backfill_for(dbs, provider))
    await dbs.chatlog.execute(SESSIONS)

    (gaps,) = await jobs.queue_startup()
    assert (gaps.kind, gaps.from_ms, gaps.to_ms, gaps.requested_by) == (GAPS, 1100, 90000, STARTUP)
    # A reconnect, or the broadcaster asking, joins the job that is waiting.
    assert await jobs.queue_startup() == []
    joined, created = await jobs.queue_gaps(CHANNEL_ID, "chat:1", actor=BROADCASTER)
    assert not created and joined is not None and joined.id == gaps.id

    await jobs.runtime.start()
    done = await until(jobs, gaps.id, "done", "failed")
    await jobs.service.writer.stop()
    assert (done.state, done.fetched, done.inserted) == ("done", 2, 2)
    assert provider.calls == [(CHANNEL_ID, 0, 60001, 1000, 0), (CHANNEL_ID, 65000, 90001, 1000, 0)]
    assert await jobs.queue_startup() == []  # the gaps are filled now
    assert await jobs.queue_gaps(CHANNEL_ID, "chat:1") == (None, False)


async def test_a_paused_provider_holds_the_job_and_it_resumes_where_it_stopped(
    dbs: Databases, make_backfill_jobs: MakeBackfillJobs
) -> None:
    """The provider spent its budget: the job waits in its step rather than failing."""
    paused = HistoryResponse(error_code=PAUSED, retry_at_ms=now_ms() + 300)
    page = HistoryResponse((privmsg(2000, "a"),), hit_limit=True)
    provider = FakeProvider(HistoryResponse((privmsg(3000, "b"),)), responses=[page, paused])
    jobs = await make_backfill_jobs(await backfill_for(dbs, provider), start=True)
    job = await jobs.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
    assert job is not None

    done = await until(jobs, job.id, "done", "failed")
    await jobs.service.writer.stop()
    assert (done.state, done.complete, done.fetched) == ("done", True, 2)
    assert provider.calls[-1] == (CHANNEL_ID, 2000, 6001, 1000, 0)  # where the pause stopped it
    assert (await jobs.runtime.get(job.id)).attempts == 0  # waited, not retried


async def test_a_running_job_stops_at_its_next_request_when_cancelled(
    dbs: Databases, make_backfill_jobs: MakeBackfillJobs
) -> None:
    """Cancel is cooperative: the request under way finishes, then the job stops (ADR-0027)."""
    provider = HeldProvider(HistoryResponse((privmsg(3000, "b"),)))
    provider.responses.append(HistoryResponse((privmsg(2000, "a"),), hit_limit=True))
    jobs = await make_backfill_jobs(await backfill_for(dbs, provider), start=True)
    job = await jobs.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
    assert job is not None
    await asyncio.wait_for(provider.asked.wait(), 5)

    cancelling = asyncio.create_task(jobs.cancel(CHANNEL_ID, job.id, actor=BROADCASTER))
    await asyncio.sleep(0.2)
    provider.answer.set()
    cancelled = await asyncio.wait_for(cancelling, 5)
    assert cancelled is not None
    cancelled = await until(jobs, job.id, "cancelled", "done", "failed")
    await jobs.service.writer.stop()
    assert (cancelled.state, cancelled.fetched, cancelled.inserted) == ("cancelled", 1, 1)
    assert len(provider.calls) == 1  # the second page was never asked for
    assert await jobs.cancel(CHANNEL_ID, job.id) is None  # finished
    # The shared connection is still usable.
    assert await jobs.queue_range(CHANNEL_ID, 7000, 8000, "chat:1") is not None


async def test_only_a_job_of_this_channel_that_hasnt_finished_can_be_cancelled(
    dbs: Databases, make_backfill_jobs: MakeBackfillJobs
) -> None:
    jobs = await make_backfill_jobs(await backfill_for(dbs, FakeProvider(HistoryResponse(lines=(PRIVMSG,)))))
    first = await jobs.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
    second = await jobs.queue_range(CHANNEL_ID, 7000, 8000, "chat:1")
    assert first is not None and second is not None

    assert await jobs.cancel("elsewhere", first.id) is None  # another channel's id
    assert await jobs.cancel(CHANNEL_ID, 999) is None
    cancelled = await jobs.cancel(CHANNEL_ID, second.id)
    assert cancelled is not None and cancelled.state == "cancelled"
    assert await jobs.cancel(CHANNEL_ID, second.id) is None
    await jobs.runtime.start()
    await until(jobs, first.id, "done")
    await jobs.service.writer.stop()
    # Open jobs first; then the finished ones, newest first.
    third = await jobs.queue_range(CHANNEL_ID, 9000, 9500, "chat:1")
    assert third is not None
    listed = [j.id for j in await jobs.jobs(CHANNEL_ID)]
    assert listed[1:] == [second.id, first.id] and listed[0] == third.id


async def test_a_channel_the_service_doesnt_log_fails_without_retrying(
    dbs: Databases, make_backfill_jobs: MakeBackfillJobs
) -> None:
    provider = FakeProvider(HistoryResponse(error_code="channel_not_logged"))
    jobs = await make_backfill_jobs(await backfill_for(dbs, provider), start=True)
    before = metrics.BACKFILL_JOBS.value(event="failed")
    job = await jobs.queue_range(CHANNEL_ID, 1100, 6000, "chat:1")
    assert job is not None
    failed = await until(jobs, job.id, "failed", "done")
    assert await counted("failed", before) == 1
    assert failed.error is not None and "doesn't log" in failed.error
    assert (await jobs.runtime.get(job.id)).attempts == 1  # failed at once, not after a backoff


async def test_jobs_an_older_image_left_queued_are_adopted_once(
    dbs: Databases, make_backfill_jobs: MakeBackfillJobs
) -> None:
    service = await backfill_for(dbs, FakeProvider())
    await dbs.chatlog.execute(SESSIONS)
    legacy = BackfillQueue(service)
    old_range = await legacy.queue_range(CHANNEL_ID, 100, 200, "chat:1")
    old_gaps, _ = await legacy.queue_gaps(CHANNEL_ID, STARTUP)
    finished = await legacy.queue_range(CHANNEL_ID, 300, 400, "chat:1")
    assert old_range is not None and old_gaps is not None and finished is not None
    await dbs.chatlog.execute("UPDATE backfill_jobs SET state = 'done' WHERE id = %s", (finished.id,))

    jobs = await make_backfill_jobs(service)
    assert await jobs.adopt_legacy() == 2
    assert await jobs.adopt_legacy() == 0  # the next start
    adopted = await jobs.jobs(CHANNEL_ID)
    assert [(j.kind, j.from_ms, j.to_ms, j.requested_by) for j in adopted] == [
        ("range", 100, 200, "chat:1"),
        (GAPS, old_gaps.from_ms, old_gaps.to_ms, STARTUP),
    ]
    # The startup pass joins the adopted gaps job instead of queueing another.
    assert await jobs.queue_startup() == []
    async with await dbs.chatlog.execute("SELECT state FROM backfill_jobs ORDER BY id") as cur:
        assert [r["state"] for r in await cur.fetchall()] == ["queued", "queued", "done"]  # only read


async def test_fill_many_reports_progress_and_stops_between_requests(dbs: Databases) -> None:
    provider = FakeProvider(HistoryResponse(lines=(PRIVMSG,)))
    service = await backfill_for(dbs, provider)
    gaps = [Gap(CHANNEL_ID, CHANNEL_LOGIN, 1100, 6000), Gap(CHANNEL_ID, CHANNEL_LOGIN, 7000, 8000)]
    reports: list[tuple[int, int]] = []
    asked = 0

    async def stop_after_one() -> bool:
        nonlocal asked
        asked += 1
        return asked > 1

    result = await service.fill_many(
        gaps, progress=lambda done, total: reports.append((done, total)), should_stop=stop_after_one
    )
    await service.writer.stop()
    assert result.stopped and result.error == STOPPED
    assert len(provider.calls) == 1
    assert reports[0] == (0, 2) and reports[-1] == (2, 2)
