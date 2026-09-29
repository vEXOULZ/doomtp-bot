"""The `triggers` module is now `automation` (ADR-0019): move its toggles and callbacks with it.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# Rows only, no tables or columns: tests/test_schema.py expects this revision to leave the catalog alone.
data_only = True


def _rename(old: str, new: str) -> None:
    # A row already under the new name wins, so neither primary key can clash.
    op.execute(
        f"UPDATE module_toggles t SET module = '{new}' WHERE module = '{old}' AND NOT EXISTS"
        f" (SELECT 1 FROM module_toggles o WHERE o.channel_id = t.channel_id AND o.module = '{new}')"
    )
    op.execute(
        f"UPDATE callbacks c SET scope = 'module:{new}' WHERE scope = 'module:{old}' AND NOT EXISTS"
        f" (SELECT 1 FROM callbacks o WHERE o.channel_id = c.channel_id AND o.kind = c.kind"
        f" AND o.scope = 'module:{new}')"
    )


def upgrade() -> None:
    _rename("triggers", "automation")


def downgrade() -> None:
    _rename("automation", "triggers")
