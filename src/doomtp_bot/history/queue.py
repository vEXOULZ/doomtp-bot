"""Backfill as queued jobs (ADR-0024 §5).

A `gaps` job fills every open gap of one channel, found when it runs, with as few requests as the service
allows (`BackfillService.fill_many`): the service takes only a start, so one request from the oldest gap
reaches the later ones too. A channel has at most one waiting, so startup, a reconnect and `!backfill gaps`
join it. A `range` job fills one range asked for by hand (`!backfill 6h`, the API). One worker takes the
oldest queued job, so jobs never run side by side and a rate-limited provider sees one caller.

Consent is checked twice: a channel with backfill off can queue nothing, and a job whose channel turned it
off while it waited is cancelled rather than run.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, fields
from typing import Any

import structlog

from doomtp_bot.clock import now_ms
from doomtp_bot.history.backfill import BackfillService, Gap
from doomtp_bot.storage.db import fetch_all, fetch_one, transaction

log = structlog.get_logger(__name__)

STARTUP = "startup"
OPEN_STATES = ("queued", "running")
GAPS, RANGE = "gaps", "range"


class BackfillRefused(Exception):
    """The channel has backfill off, is not active, or the range makes no sense."""


@dataclass(frozen=True, slots=True)
class BackfillJob:
    id: int
    channel_id: str
    from_ms: int
    to_ms: int
    requested_by: str
    requested_at: int
    state: str
    started_at: int | None = None
    finished_at: int | None = None
    fetched: int = 0
    inserted: int = 0
    complete: bool | None = None
    error: str | None = None
    kind: str = RANGE

    @classmethod
    def of(cls, row: dict[str, Any]) -> BackfillJob:
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    def to_json(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


class BackfillQueue:
    def __init__(self, service: BackfillService) -> None:
        self.service = service
        self.conn = service.conn
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # ── queueing ──
    async def queue_range(
        self, channel_id: str, from_ms: int, to_ms: int, requested_by: str
    ) -> BackfillJob | None:
        """Queue one range. None when the same range is already queued or running."""
        if self.service.login_if_enabled(channel_id) is None:
            raise BackfillRefused("backfill is off for this channel")
        to_ms = min(to_ms, now_ms())
        if from_ms < 0 or to_ms <= from_ms:
            raise BackfillRefused("the range must end after it starts, and not in the future")
        async with transaction(self.conn):
            row = await fetch_one(
                self.conn,
                "INSERT INTO backfill_jobs (channel_id, from_ms, to_ms, requested_by, requested_at)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (channel_id, from_ms, to_ms) WHERE kind = 'range' AND state IN ('queued', 'running')"
                " DO NOTHING RETURNING *",
                (channel_id, from_ms, to_ms, requested_by, now_ms()),
            )
        if row is None:
            return None
        self._wake.set()
        job = BackfillJob.of(row)
        log.info("history.job_queued", job=job.id, channel=channel_id, by=requested_by)
        return job

    async def queue_gaps(self, channel_id: str, requested_by: str) -> tuple[BackfillJob | None, bool]:
        """The channel's one job for its open gaps, and whether it is new. A job already waiting takes in the
        gaps found since (it looks for them when it runs); None when no gap is open."""
        login = self.service.login_if_enabled(channel_id)
        if login is None:
            raise BackfillRefused("backfill is off for this channel")
        gaps = await self.service.open_gaps(channel_id, login)
        if not gaps:
            return None, False
        async with transaction(self.conn):
            row = await fetch_one(
                self.conn,
                "INSERT INTO backfill_jobs (channel_id, kind, from_ms, to_ms, requested_by, requested_at)"
                " VALUES (%s, 'gaps', %s, %s, %s, %s)"
                " ON CONFLICT (channel_id) WHERE kind = 'gaps' AND state = 'queued' DO UPDATE SET"
                " from_ms = LEAST(backfill_jobs.from_ms, EXCLUDED.from_ms),"
                " to_ms = GREATEST(backfill_jobs.to_ms, EXCLUDED.to_ms)"
                " RETURNING *, xmax = 0 AS created",
                (channel_id, gaps[0].from_ms, max(g.to_ms for g in gaps), requested_by, now_ms()),
            )
        assert row is not None
        job = BackfillJob.of(row)
        if row["created"]:
            self._wake.set()
            log.info("history.job_queued", job=job.id, channel=channel_id, by=requested_by, gaps=len(gaps))
        return job, bool(row["created"])

    async def queue_startup(self) -> list[BackfillJob]:
        """A job for each opted-in channel with open gaps (ADR-0024 §5). Before the worker first starts, a job the last
        stop cut short is put back too; on a reconnect the worker is still running its job."""
        if self._task is None:
            async with transaction(self.conn):
                await self.conn.execute(
                    "UPDATE backfill_jobs SET state = 'queued', started_at = NULL WHERE state = 'running'"
                )
        jobs: list[BackfillJob] = []
        for channel_id, _ in self.service.enabled_channels():
            job, created = await self.queue_gaps(channel_id, STARTUP)
            if job is not None and created:
                jobs.append(job)
        self._wake.set()  # interrupted jobs are waiting too
        return jobs

    # ── reading and cancelling ──
    async def jobs(self, channel_id: str, *, limit: int = 20) -> list[BackfillJob]:
        """Queued and running jobs first, oldest first; then the latest finished ones."""
        rows = await fetch_all(
            self.conn,
            "SELECT * FROM backfill_jobs WHERE channel_id = %s"
            " ORDER BY state IN ('queued', 'running') DESC,"
            " CASE WHEN state IN ('queued', 'running') THEN id ELSE -id END LIMIT %s",
            (channel_id, limit),
        )
        return [BackfillJob.of(r) for r in rows]

    async def cancel(self, channel_id: str, job_id: int) -> BackfillJob | None:
        """Cancel a queued job. A running one finishes: the provider is already being asked."""
        async with transaction(self.conn):
            row = await fetch_one(
                self.conn,
                "UPDATE backfill_jobs SET state = 'cancelled', finished_at = %s"
                " WHERE id = %s AND channel_id = %s AND state = 'queued' RETURNING *",
                (now_ms(), job_id, channel_id),
            )
        return None if row is None else BackfillJob.of(row)

    # ── running ──
    async def run_next(self) -> BackfillJob | None:
        """Take the oldest queued job and run it. None when the queue is empty."""
        async with transaction(self.conn):
            row = await fetch_one(
                self.conn,
                "UPDATE backfill_jobs SET state = 'running', started_at = %s WHERE id = ("
                " SELECT id FROM backfill_jobs WHERE state = 'queued' ORDER BY id LIMIT 1"
                " FOR UPDATE SKIP LOCKED) RETURNING *",
                (now_ms(),),
            )
        if row is None:
            return None
        job = BackfillJob.of(row)
        login = self.service.login_if_enabled(job.channel_id)
        if login is None:
            return await self._finish(job, "cancelled", error="backfill is off for this channel")
        try:
            if job.kind == GAPS:
                gaps = await self.service.open_gaps(job.channel_id, login)  # the ones still open now
            else:
                gaps = [Gap(job.channel_id, login, job.from_ms, job.to_ms)]
            outcome = await self.service.fill_many(gaps)
        except Exception as exc:
            log.exception("history.job_failed", job=job.id)
            return await self._finish(job, "failed", error=repr(exc))
        return await self._finish(
            job,
            "failed" if outcome.error else "done",
            fetched=outcome.fetched,
            inserted=outcome.inserted,
            complete=outcome.complete,
            error=outcome.error or None,
        )

    async def drain(self) -> list[BackfillJob]:
        """Run jobs until none is queued."""
        done = []
        while (job := await self.run_next()) is not None:
            done.append(job)
        return done

    async def _finish(
        self,
        job: BackfillJob,
        state: str,
        *,
        fetched: int = 0,
        inserted: int = 0,
        complete: bool | None = None,
        error: str | None = None,
    ) -> BackfillJob:
        async with transaction(self.conn):
            row = await fetch_one(
                self.conn,
                "UPDATE backfill_jobs SET state = %s, finished_at = %s, fetched = %s, inserted = %s,"
                " complete = %s, error = %s WHERE id = %s RETURNING *",
                (state, now_ms(), fetched, inserted, complete, error, job.id),
            )
        assert row is not None
        return BackfillJob.of(row)

    # ── the worker ──
    def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._work(), name="history-backfill-queue")

    async def stop(self) -> None:
        """Let the running job finish, if any, then stop; an idle worker stops at once.

        The worker is asked, never cancelled: it shares the connection with the rest of the process, and
        a cancel that lands while psycopg enters or leaves a savepoint leaves the connection's nesting
        count wrong, so the next transaction on it fails. The provider's timeout bounds the wait. A job
        a crash cuts short stays `running` and is queued again at the next startup.
        """
        if self._task is not None:
            self._stopping.set()
            self._wake.set()
            await self._task
            self._task = None

    async def _work(self) -> None:
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                job = await self.run_next()
            except Exception:
                log.exception("history.queue_failed")
                # The database is in trouble: try again later, without spinning, unless asked to stop.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), 30)
                continue
            if job is None:
                await self._wake.wait()
