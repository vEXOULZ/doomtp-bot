"""Trigger cooldowns (ADR-0031).

A trigger has a whole-channel cooldown and a per-chatter one, in seconds, like a command's shared and
personal buckets. 0 is none, which is what every existing trigger keeps.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE triggers"
        " ADD COLUMN cooldown_s integer NOT NULL DEFAULT 0 CHECK (cooldown_s BETWEEN 0 AND 86400),"
        " ADD COLUMN user_cooldown_s integer NOT NULL DEFAULT 0 CHECK (user_cooldown_s BETWEEN 0 AND 86400)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE triggers DROP COLUMN user_cooldown_s, DROP COLUMN cooldown_s")
