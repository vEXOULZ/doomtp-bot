"""`channels.public_log`: whether anyone may read and search the channel's chat log (ADR-0026).

On by default, so the log a channel already keeps is public until a moderator turns it off.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE channels ADD COLUMN public_log boolean NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE channels DROP COLUMN public_log")
