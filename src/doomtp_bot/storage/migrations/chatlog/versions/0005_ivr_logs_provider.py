"""logs.ivr.fi is the history service (ADR-0008, amended 2026-09-30).

What it brings in is logged with `source = 'ivr-logs'`; rows recent-messages filled keep their source.
`backfill_runs.reached_ms` is the newest line a stopped fill stored, so the next one resumes there.

Runs recent-messages recorded `out_of_reach` no longer settle a gap: that service kept about a day, and
logs.ivr.fi keeps far more, so those gaps are open again.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE messages DROP CONSTRAINT messages_source_check, ADD CONSTRAINT messages_source_check"
        " CHECK (source IN ('eventsub', 'ivr-logs', 'recent-messages'))"
    )
    op.execute("ALTER TABLE backfill_runs ALTER COLUMN provider SET DEFAULT 'ivr-logs'")
    op.execute("ALTER TABLE backfill_runs ADD COLUMN reached_ms bigint")


def downgrade() -> None:
    op.execute("ALTER TABLE backfill_runs DROP COLUMN reached_ms")
    op.execute("ALTER TABLE backfill_runs ALTER COLUMN provider SET DEFAULT 'recent-messages'")
    # The old revision knows one history service: what ivr.fi filled is counted as it.
    for table in ("messages", "chat_notifications", "mod_events"):
        op.execute(f"UPDATE {table} SET source = 'recent-messages' WHERE source = 'ivr-logs'")
    op.execute(
        "ALTER TABLE messages DROP CONSTRAINT messages_source_check, ADD CONSTRAINT messages_source_check"
        " CHECK (source IN ('eventsub', 'recent-messages'))"
    )
