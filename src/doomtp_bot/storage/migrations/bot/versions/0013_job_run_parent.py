"""vex-platform 0.6.0's `job_runs.parent_id` column (ADR-0027).

0.6.0 records which run queued a run (a step's `enqueue`), for `GET /api/v2/jobs/{id}/related` and the
`parent` filter. It reads the column, so this runs before that version starts; an image still on 0.5.0
ignores it. The bot's jobs queue no others yet, so it stays NULL here.

Revision ID: 0013
Revises: 0012
"""

from alembic import op
from vex_platform import migrations

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrations.apply(op, migrations.jobs_sql(3, schema="jobs"))


def downgrade() -> None:
    op.execute("ALTER TABLE jobs.job_runs DROP COLUMN parent_id")
