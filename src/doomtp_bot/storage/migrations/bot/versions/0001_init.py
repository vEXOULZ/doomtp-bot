"""The first bot schema: channels, roles, toggles, custom commands, variables, automation. Written as SQL before ADR-0022; the file is unchanged.

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
    run_sql("bot", "0001_init.sql")
    record_legacy(1, "0001_init.sql")


def downgrade() -> None:
    op.execute("DROP TABLE audit_log")
    op.execute("DROP TABLE filters")
    op.execute("DROP TABLE triggers")
    op.execute("DROP TABLE publication_write_grants")
    op.execute("DROP TABLE variables")
    op.execute("DROP TABLE custom_command_pack_publications")
    op.execute("DROP TABLE custom_command_pack_members")
    op.execute("DROP TABLE custom_command_packs")
    op.execute("DROP TABLE custom_command_publications")
    op.execute("DROP TABLE custom_command_links")
    op.execute("DROP TABLE custom_command_versions")
    op.execute("DROP TABLE custom_commands")
    op.execute("DROP TABLE ignore_list")
    op.execute("DROP TABLE callbacks")
    op.execute("DROP TABLE cooldown_rules")
    op.execute("DROP TABLE command_rules")
    op.execute("DROP TABLE command_toggles")
    op.execute("DROP TABLE module_toggles")
    op.execute("DROP TABLE global_admins")
    op.execute("DROP TABLE role_members")
    op.execute("DROP TABLE roles")
    op.execute("DROP TABLE channels")
    op.execute("DROP TABLE api_keys")
    op.execute("DROP TABLE oauth_tokens")
    forget_legacy(1)
    op.execute("DROP TABLE schema_migrations")
