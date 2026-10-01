"""vex-platform 0.3.0's `job_runs.scope` column (ADR-0027).

0.3.0 keeps the channel a run was queued for and gives it to every audit row about the run (`job.cancel`,
`job.pause` ...), not only `job.enqueue`'s, so a moderator's `/api/v2/audit` shows the whole life of their
channel's backfill jobs. It reads the column, so this runs before that version starts; an image still on
0.2.0 ignores it. Runs queued before it keep a NULL scope.

Revision ID: 0012
Revises: 0011
"""

from alembic import op
from vex_platform import migrations

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrations.apply(op, migrations.jobs_sql(2, schema="jobs"))


def downgrade() -> None:
    op.execute("ALTER TABLE jobs.job_runs DROP COLUMN scope")
