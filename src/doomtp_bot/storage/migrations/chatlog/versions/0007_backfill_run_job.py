"""`backfill_runs.job_id`: the `chat_backfill` job run that recorded the row, so a coverage gap links to
it (ADR-0027). NULL for rows from before, and for fills no job ran.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE backfill_runs ADD COLUMN job_id bigint")


def downgrade() -> None:
    op.execute("ALTER TABLE backfill_runs DROP COLUMN job_id")
