"""One backfill job per channel for its gaps (ADR-0024 §5, amended 2026-09-29).

A `gaps` job fills every open gap of its channel when it runs, with one request from the oldest one;
its range only says what was open when it was queued. A channel has at most one such job waiting
(`backfill_jobs_gaps_queued`), so startup, a reconnect and `!backfill gaps` join it rather than queue
another. A `range` job is one asked for by hand, as before.

The per-gap jobs startup queued before this are cancelled: the next startup queues one job instead.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE backfill_jobs ADD COLUMN kind text NOT NULL DEFAULT 'range' CHECK (kind IN ('range', 'gaps'))"
    )
    op.execute(
        "UPDATE backfill_jobs SET state = 'cancelled', error = 'replaced by one job per channel'"
        " WHERE state = 'queued' AND requested_by = 'startup'"
    )
    op.execute("DROP INDEX backfill_jobs_open")
    op.execute(
        "CREATE UNIQUE INDEX backfill_jobs_open ON backfill_jobs (channel_id, from_ms, to_ms)"
        " WHERE kind = 'range' AND state IN ('queued', 'running')"
    )
    op.execute(
        "CREATE UNIQUE INDEX backfill_jobs_gaps_queued ON backfill_jobs (channel_id)"
        " WHERE kind = 'gaps' AND state = 'queued'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX backfill_jobs_gaps_queued")
    op.execute("DROP INDEX backfill_jobs_open")
    op.execute(
        "UPDATE backfill_jobs SET state = 'cancelled', error = 'one job per channel was undone'"
        " WHERE kind = 'gaps' AND state IN ('queued', 'running')"
    )
    op.execute(
        "CREATE UNIQUE INDEX backfill_jobs_open ON backfill_jobs (channel_id, from_ms, to_ms)"
        " WHERE state IN ('queued', 'running')"
    )
    op.execute("ALTER TABLE backfill_jobs DROP COLUMN kind")
