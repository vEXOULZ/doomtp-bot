"""Quotes.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

from doomtp_bot.storage.schema import forget_legacy, record_legacy, run_sql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql("bot", "0002_quotes.sql")
    record_legacy(2, "0002_quotes.sql")


def downgrade() -> None:
    op.execute("DROP TABLE quotes")
    forget_legacy(2)
