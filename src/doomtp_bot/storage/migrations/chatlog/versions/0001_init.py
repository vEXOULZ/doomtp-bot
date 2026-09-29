"""The chat log: users, messages, events, command runs. Written as SQL before ADR-0022; the file is unchanged.

Revision ID: 0001
Revises: <base>
"""

from alembic import op

from doomtp_bot.storage.schema import forget_legacy, record_legacy, run_sql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql("chatlog", "0001_init.sql")
    record_legacy(1, "0001_init.sql")


def downgrade() -> None:
    op.execute("DROP TABLE outbound_msgs")
    op.execute("DROP TABLE command_runs")
    op.execute("DROP TABLE backfill_runs")
    op.execute("DROP TABLE log_sessions")
    op.execute("DROP TABLE mod_events")
    op.execute("DROP TABLE chat_notifications")
    op.execute("DROP TABLE messages")
    op.execute("DROP TABLE user_names")
    op.execute("DROP TABLE users")
    op.execute("DROP FUNCTION chatlog_unaccent(text)")
    forget_legacy(1)
    op.execute("DROP TABLE schema_migrations")
