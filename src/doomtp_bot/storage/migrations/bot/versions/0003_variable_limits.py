"""Storage limits per namespace owner.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

from doomtp_bot.storage.schema import forget_legacy, record_legacy, run_sql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql("bot", "0003_variable_limits.sql")
    record_legacy(3, "0003_variable_limits.sql")


def downgrade() -> None:
    op.execute("DROP TABLE variable_limits")
    op.execute("ALTER TABLE variables DROP COLUMN size_bytes")
    forget_legacy(3)
