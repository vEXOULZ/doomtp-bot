"""Pack member ranks (ADR-0033).

A pack can set the rank a member needs wherever the pack is published. A channel's `!perm` can raise
it there, never lower it. NULL is everyone, which is what every existing member keeps.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE custom_command_pack_members ADD COLUMN role text"
        " CHECK (role IN ('subscriber', 'vip', 'moderator', 'lead_moderator', 'broadcaster'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE custom_command_pack_members DROP COLUMN role")
