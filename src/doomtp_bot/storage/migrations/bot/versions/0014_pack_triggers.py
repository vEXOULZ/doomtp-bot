"""Triggers owned by a pack (ADR-0029).

A pack trigger has `channel_id = '*'`, the pack it belongs to, and a key unique within the pack, which the
pack's install script matches on. It fires in every channel where the pack is published and its module is
on.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE triggers"
        " ADD COLUMN pack_id text REFERENCES custom_command_packs(id) ON DELETE CASCADE,"
        " ADD COLUMN pack_key text"
    )
    op.execute("CREATE UNIQUE INDEX ux_triggers_pack_key ON triggers(pack_id, pack_key) WHERE pack_id IS NOT NULL")


def downgrade() -> None:
    op.execute("DELETE FROM triggers WHERE pack_id IS NOT NULL")
    op.execute("DROP INDEX ux_triggers_pack_key")
    op.execute("ALTER TABLE triggers DROP COLUMN pack_key, DROP COLUMN pack_id")
