"""vex-platform's tables: the `jobs` schema and `public.audit_log` (ADR-0027).

`jobs` is a Postgres schema of its own: procrastinate 3.10.0's tables plus `job_runs` and
`job_run_events`. `public.audit_log` is the audit table shared with twitch-archive; `bot.audit_log`
keeps its name, so it is always written qualified. Nothing reads or writes either yet.

The SQL is vex-platform's, frozen per revision: `jobs_sql(1)` creates the same schema whichever
vex-platform version is installed. It puts the search path back when it is done.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
from vex_platform import migrations

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrations.apply(op, migrations.jobs_sql(1, schema="jobs"))
    migrations.apply(op, migrations.audit_sql(1, table="public.audit_log"))


def downgrade() -> None:
    op.execute("DROP TABLE public.audit_log")
    op.execute("DROP SCHEMA jobs CASCADE")
