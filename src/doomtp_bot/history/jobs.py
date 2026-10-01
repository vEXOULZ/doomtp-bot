"""Backfill as vex-platform's `chat_backfill` job kind (ADR-0024 §5, ADR-0027).

A `gaps` job fills every open gap of one channel, found when it runs (`BackfillService.fill_many`). A
channel has at most one waiting (its `queued_key`), so startup, a reconnect and `!backfill gaps` join it. A
`range` job fills one range asked for by hand (`!backfill 6h`, the API); the same range is never queued or
running twice (its `active_key`). Every job takes the same lock, so jobs never run side by side and the
rate-limited provider sees one caller.

The runtime keeps the runs in `jobs.job_runs`, audits what is done to them and retries a job whose fill
failed. When the provider pauses (its daily budget is spent, or the service kept failing), the job waits
in its step until the provider asks again, then resumes where it stopped. A cancel or a shutdown is seen
between two requests: the step never stops mid-write, since it writes through the bot's shared `chatlog`
connection. A shutdown queues the job again at the next start.

Consent is checked twice: a channel with backfill off can queue nothing, and a job whose channel turned it
off while it waited is cancelled rather than run.

`BackfillJobs` answers in the shape `BackfillQueue` did (`BackfillJob`), so `!backfill` and the v1 routes
keep their replies.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, fields
from typing import Any, Final

import structlog
from vex_platform.actor import SYSTEM, Actor
from vex_platform.jobs import (
    FINISHED,
    STATES,
    Enqueued,
    JobConflict,
    JobKind,
    JobNotFound,
    JobRun,
    JobRuntime,
    Registry,
    RunStopped,
    StepContext,
    StepError,
    StepRefused,
)

from doomtp_bot.clock import now_ms
from doomtp_bot.core import metrics
from doomtp_bot.history.backfill import BackfillService, Gap
from doomtp_bot.history.provider import NOT_LOGGED
from doomtp_bot.storage.db import fetch_all

log = structlog.get_logger(__name__)

KIND: Final = "chat_backfill"
STEP: Final = "chat_backfill.fill"
LOCK: Final = "history-provider"  # one job at a time, whichever channel it is for
STARTUP = "startup"
GAPS, RANGE = "gaps", "range"
OPEN_STATES = ("queued", "running", "paused")
OFF = "backfill is off for this channel"
PAUSE_POLL_S = 1.0  # how often a job waiting out a provider pause looks for a cancel
CANCEL_WAIT_S = 2.0  # how long a cancel waits for a running job to stop before answering


class BackfillRefused(Exception):
    """The channel has backfill off, is not active, or the range makes no sense."""


def _ms(at: dt.datetime | None) -> int | None:
    return None if at is None else round(at.timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class BackfillJob:
    """One job as `!backfill` and the v1 routes show it: times in ms, and `done` for a job that succeeded."""

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
        """From a `chatlog.backfill_jobs` row."""
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def of_run(cls, run: JobRun) -> BackfillJob:
        """From a `chat_backfill` run."""
        p = run.payload
        return cls(
            id=run.id,
            channel_id=p["channel_id"],
            from_ms=p["from_ms"],
            to_ms=p["to_ms"],
            requested_by=p.get("requested_by", ""),
            requested_at=_ms(run.created_at) or 0,
            state="done" if run.state == "succeeded" else run.state,
            started_at=_ms(run.started_at),
            finished_at=_ms(run.finished_at),
            fetched=p.get("fetched", 0),
            inserted=p.get("inserted", 0),
            complete=p.get("complete"),
            error=run.last_error or p.get("error"),
            kind=p["kind"],
        )

    def to_json(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def subject(channel_id: str) -> str:
    return f"channel:{channel_id}"


def _widen(queued: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """A second gaps job for a channel joins the queued one: its range only says what the job covers."""
    return {
        **queued,
        "from_ms": min(queued["from_ms"], new["from_ms"]),
        "to_ms": max(queued["to_ms"], new["to_ms"]),
    }


def count(event: str, run: JobRun) -> None:
    """A runtime hook: count what happens to backfill jobs (started, paused, retrying, requeued, and how
    each ended) on `/metrics` (ADR-0015)."""
    if run.kind == KIND:
        metrics.BACKFILL_JOBS.inc(event=event)


def register(registry: Registry, service: BackfillService) -> JobKind:
    """Add the `chat_backfill` kind, whose one step fills with `service`."""

    async def fill(ctx: StepContext) -> None:
        p = ctx.payload
        channel_id = p["channel_id"]
        while True:
            login = service.login_if_enabled(channel_id)
            if login is None:
                p["error"] = OFF
                ctx.log.warning("%s: cancelled", OFF)
                await ctx.runtime.cancel(ctx.run_id, wait=0)
                raise RunStopped()
            if p["kind"] == GAPS:
                gaps = await service.open_gaps(channel_id, login)  # the ones still open now
            else:
                gaps = [Gap(channel_id, login, p["from_ms"], p["to_ms"])]
            result = await service.fill_many(
                gaps,
                progress=lambda done, total: ctx.progress(done, total, "items"),  # gaps
                should_stop=ctx.should_stop,
            )
            p["fetched"] = p.get("fetched", 0) + result.fetched
            p["inserted"] = p.get("inserted", 0) + result.inserted
            p["complete"] = result.complete
            if result.stopped:
                raise RunStopped()
            if result.retry_at_ms is None:
                break
            ctx.log.info("the history service paused until %s", dt.datetime.fromtimestamp(
                result.retry_at_ms / 1000, dt.UTC).isoformat())  # fmt: skip
            await ctx.save()
            while (wait_ms := result.retry_at_ms - now_ms()) > 0:
                await ctx.check_stop()
                await asyncio.sleep(min(PAUSE_POLL_S, wait_ms / 1000))
        if result.error == NOT_LOGGED:
            raise StepRefused("the history service doesn't log this channel")
        if result.error:
            raise StepError(result.error)  # retried, resuming where the fill stopped

    registry.add_step(STEP, fill)
    return registry.kind(
        KIND,
        [STEP],
        description="Fetch chat the bot missed from the history service into the log (ADR-0008).",
        lock=lambda run: LOCK,
        cancel_mode="cooperative",
    )


class BackfillJobs:
    """Queue, list and cancel `chat_backfill` runs for one channel at a time."""

    def __init__(self, service: BackfillService, runtime: JobRuntime) -> None:
        self.service = service
        self.runtime = runtime
        if count not in runtime.hooks:
            runtime.hooks.append(count)

    # ── queueing ──
    async def queue_range(
        self, channel_id: str, from_ms: int, to_ms: int, requested_by: str, *, actor: Actor = SYSTEM
    ) -> BackfillJob | None:
        """Queue one range. None when the same range is already queued or running."""
        if self.service.login_if_enabled(channel_id) is None:
            raise BackfillRefused(OFF)
        to_ms = min(to_ms, now_ms())
        if from_ms < 0 or to_ms <= from_ms:
            raise BackfillRefused("the range must end after it starts, and not in the future")
        payload = self._payload(channel_id, RANGE, from_ms, to_ms, requested_by)
        enqueued = await self.runtime.enqueue(
            KIND,
            subject(channel_id),
            payload,
            actor=actor,
            active_key=f"{RANGE}:{channel_id}:{from_ms}:{to_ms}",
            scope=channel_id,
        )
        if not enqueued.created:
            return None
        log.info("history.job_queued", job=enqueued.run.id, channel=channel_id, by=requested_by)
        return BackfillJob.of_run(enqueued.run)

    async def queue_gaps(
        self, channel_id: str, requested_by: str, *, actor: Actor = SYSTEM
    ) -> tuple[BackfillJob | None, bool]:
        """The channel's one job for its open gaps, and whether it is new. A job already waiting takes in the
        gaps found since (it looks for them when it runs); None when no gap is open."""
        login = self.service.login_if_enabled(channel_id)
        if login is None:
            raise BackfillRefused(OFF)
        gaps = await self.service.open_gaps(channel_id, login)
        if not gaps:
            return None, False
        payload = self._payload(channel_id, GAPS, gaps[0].from_ms, max(g.to_ms for g in gaps), requested_by)
        enqueued = await self._queue_gaps(channel_id, payload, actor)
        job = BackfillJob.of_run(enqueued.run)
        if enqueued.created:
            log.info("history.job_queued", job=job.id, channel=channel_id, by=requested_by, gaps=len(gaps))
        return job, enqueued.created

    async def queue_startup(self) -> list[BackfillJob]:
        """A job for each opted-in channel with open gaps (ADR-0024 §5). A job the last stop cut short is
        queued again by the runtime when it starts."""
        jobs: list[BackfillJob] = []
        for channel_id, _ in self.service.enabled_channels():
            job, created = await self.queue_gaps(channel_id, STARTUP)
            if job is not None and created:
                jobs.append(job)
        return jobs

    async def adopt_legacy(self) -> int:
        """Queue a run for each job `chatlog.backfill_jobs` still has queued or running, from an image
        before ADR-0027, and return how many. Each run keeps its job's id as `legacy_id`, so a later start
        doesn't queue it twice; the old table is only read."""
        rows = await fetch_all(
            self.service.conn,
            "SELECT * FROM backfill_jobs WHERE state IN ('queued', 'running') ORDER BY id",
        )
        adopted = 0
        for row in rows:
            if await self.runtime.find(KIND, payload_contains={"legacy_id": row["id"]}, states=STATES):
                continue
            job = BackfillJob.of(row)
            payload = self._payload(job.channel_id, job.kind, job.from_ms, job.to_ms, job.requested_by)
            payload["legacy_id"] = job.id
            if job.kind == GAPS:
                enqueued = await self._queue_gaps(job.channel_id, payload, SYSTEM)
            else:
                enqueued = await self.runtime.enqueue(
                    KIND,
                    subject(job.channel_id),
                    payload,
                    active_key=f"{RANGE}:{job.channel_id}:{job.from_ms}:{job.to_ms}",
                    scope=job.channel_id,
                )
            adopted += enqueued.created
        if adopted:
            log.info("history.legacy_jobs_adopted", jobs=adopted)
        return adopted

    async def _queue_gaps(self, channel_id: str, payload: dict[str, Any], actor: Actor) -> Enqueued:
        return await self.runtime.enqueue(
            KIND,
            subject(channel_id),
            payload,
            actor=actor,
            queued_key=f"{GAPS}:{channel_id}",
            on_duplicate="merge",
            merge=_widen,
            scope=channel_id,
        )

    @staticmethod
    def _payload(channel_id: str, kind: str, from_ms: int, to_ms: int, requested_by: str) -> dict[str, Any]:
        return {
            "channel_id": channel_id,
            "kind": kind,
            "from_ms": from_ms,
            "to_ms": to_ms,
            "requested_by": requested_by,
        }

    # ── reading and cancelling ──
    async def jobs(self, channel_id: str, *, limit: int = 20) -> list[BackfillJob]:
        """Queued, running and paused jobs first, oldest first; then the latest finished ones."""
        waiting = await self.runtime.find(KIND, subject(channel_id), states=OPEN_STATES)
        finished = await self.runtime.list(
            kind=KIND, subject=subject(channel_id), states=FINISHED, limit=limit
        )
        return [BackfillJob.of_run(run) for run in [*reversed(waiting), *finished][:limit]]

    async def cancel(self, channel_id: str, job_id: int, *, actor: Actor = SYSTEM) -> BackfillJob | None:
        """Cancel a job that hasn't finished. A running one stops before its next request; the answer is
        the job as it is once stopped, or still running if that takes longer than `CANCEL_WAIT_S`. None for
        a job of another channel, or one already finished."""
        try:
            run = await self.runtime.get(job_id)
            if run.kind != KIND or run.subject != subject(channel_id) or not run.active:
                return None
            run = await self.runtime.cancel(job_id, actor=actor, wait=CANCEL_WAIT_S)
        except (JobNotFound, JobConflict):
            return None
        return BackfillJob.of_run(run)
