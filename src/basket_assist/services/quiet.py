"""Тихие часы (ADR 0008): с 22:00 до 10:00 по Москве бот сам ничего не пишет — ни в беседу, ни в личку.

Накопившееся уходит в 10:00: задачи, которые проверяют is_quiet, просто ждут следующей минуты вне тихих
часов (состояние — в БД, поэтому перезапуск ничего не теряет). Не касается сводок капитану, ответов на
действия пользователя и правок анонса (правка уведомлений не даёт).
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

QUIET_TZ = ZoneInfo("Europe/Moscow")
QUIET_FROM, QUIET_TO = 22, 10  # часы по Москве


def is_quiet(now: datetime) -> bool:
    """now — UTC (с tzinfo или без)."""
    aware = now if now.tzinfo else now.replace(tzinfo=UTC)
    hour = aware.astimezone(QUIET_TZ).hour
    return hour >= QUIET_FROM or hour < QUIET_TO
