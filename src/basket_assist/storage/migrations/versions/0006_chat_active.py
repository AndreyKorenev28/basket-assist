"""chat active

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20 09:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("chat", schema=None) as batch_op:
        batch_op.add_column(sa.Column("active", sa.Boolean(), server_default="1", nullable=False))
    # какая беседа активна, при старте решает bootstrap по config/tenant.toml (vk_chat_peer_id)


def downgrade() -> None:
    with op.batch_alter_table("chat", schema=None) as batch_op:
        batch_op.drop_column("active")
