"""match time source: время матча, введённое руководителем

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19 00:20:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("match", schema=None) as batch_op:
        batch_op.add_column(sa.Column("time_source", sa.String(length=10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("match", schema=None) as batch_op:
        batch_op.drop_column("time_source")
