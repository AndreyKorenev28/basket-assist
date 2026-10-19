"""Ключи журнала напоминаний и сводок: «id матча» → «id матча:дата» (weekly.cycle_key)

Без этого после обновления бот не узнал бы уже отправленные сводки и прислал бы их повторно.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

KINDS = "('remind30', 'digest24', 'digest24_skipped', 'digest2')"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE notification_log
        SET dedup_key = dedup_key || ':' || (SELECT match_date FROM match WHERE match.id = notification_log.match_id)
        WHERE kind IN {KINDS}
          AND match_id IS NOT NULL
          AND dedup_key = CAST(match_id AS TEXT)
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE notification_log
        SET dedup_key = CAST(match_id AS TEXT)
        WHERE kind IN {KINDS} AND match_id IS NOT NULL AND dedup_key LIKE CAST(match_id AS TEXT) || ':%'
        """
    )
