"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None


def upgrade() -> None:
    ${upgrades if upgrades else 'op.execute("")'}


def downgrade() -> None:
    # Every revision needs one (ADR-0022), and CI runs it: undo exactly what upgrade() did.
    ${downgrades if downgrades else 'op.execute("")'}
