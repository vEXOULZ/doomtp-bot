"""The hosts `http get` may fetch, their secrets and the request limits (ADR-0020).

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One global list: only bot admins' commands can call `http`, so channels don't keep their own.
    # A secret belongs to its host and goes with it; secret_value is never read back out of the bot.
    op.execute(
        """
        CREATE TABLE http_hosts (
            pattern      text PRIMARY KEY,
            plain_http   boolean NOT NULL DEFAULT false,
            secret_kind  text CHECK (secret_kind IN ('query', 'header')),
            secret_name  text,
            secret_value text,
            added_at     bigint NOT NULL,
            added_by     text,
            CHECK ((secret_kind IS NULL) = (secret_name IS NULL)
                   AND (secret_kind IS NULL) = (secret_value IS NULL))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE http_limits (
            id                 boolean PRIMARY KEY DEFAULT true CHECK (id),
            channel_per_minute integer NOT NULL CHECK (channel_per_minute >= 0),
            host_per_minute    integer NOT NULL CHECK (host_per_minute >= 0),
            updated_at         bigint NOT NULL,
            updated_by         text
        )
        """
    )
    op.execute("INSERT INTO http_limits (channel_per_minute, host_per_minute, updated_at) VALUES (10, 60, 0)")


def downgrade() -> None:
    op.execute("DROP TABLE http_limits")
    op.execute("DROP TABLE http_hosts")
