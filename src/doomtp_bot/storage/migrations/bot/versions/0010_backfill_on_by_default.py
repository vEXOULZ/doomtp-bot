"""`channels.history_backfill` is on for a channel joined from now on (ADR-0008, amended 2026-09-30).

Only the default changes: a channel already joined keeps whatever it has.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE channels ALTER COLUMN history_backfill SET DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE channels ALTER COLUMN history_backfill SET DEFAULT false")
