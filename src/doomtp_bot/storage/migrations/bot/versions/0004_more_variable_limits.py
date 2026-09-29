"""List and name limits per owner.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

from doomtp_bot.storage.schema import forget_legacy, record_legacy, run_sql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql("bot", "0004_more_variable_limits.sql")
    record_legacy(4, "0004_more_variable_limits.sql")


def downgrade() -> None:
    op.execute("ALTER TABLE variable_limits DROP COLUMN list_items, DROP COLUMN names_per_space")
    forget_legacy(4)
