"""Internal pack members and system packs.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

from doomtp_bot.storage.schema import forget_legacy, record_legacy, run_sql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql("bot", "0005_internal_and_system_packs.sql")
    record_legacy(5, "0005_internal_and_system_packs.sql")


def downgrade() -> None:
    op.execute("DROP INDEX ux_cc_system_pack_name")
    op.execute("ALTER TABLE custom_command_packs DROP COLUMN system_version")
    op.execute("ALTER TABLE custom_command_pack_members DROP COLUMN internal")
    forget_legacy(5)
