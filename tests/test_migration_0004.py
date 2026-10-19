"""Миграция 0004: старые ключи журнала сводок переписываются в формат weekly.cycle_key."""

from datetime import date

import sqlalchemy as sa
from alembic import command

from basket_assist.storage.db import alembic_config


def test_old_digest_keys_become_cycle_keys(tmp_path):
    path = (tmp_path / "m.db").as_posix()
    cfg = alembic_config(f"sqlite+aiosqlite:///{path}")
    command.upgrade(cfg, "0003")
    eng = sa.create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO tenant (id, key, name) VALUES (1, 't', 'T')"))
        c.execute(
            sa.text(
                "INSERT INTO league (id, key, name, platform, base_url, timezone, min_players, max_players, "
                "poll_weekday, poll_time, store_birth_date) VALUES (1, 'l', 'L', 'mtgame', 'u', 'Europe/Moscow', 5, 12, 2, '10:00', 0)"
            )
        )
        c.execute(sa.text("INSERT INTO tournament (id, league_id, external_id) VALUES (1, 1, 't')"))
        c.execute(
            sa.text("INSERT INTO league_team (id, league_id, external_id, name) VALUES (1, 1, 'a', 'A')")
        )
        c.execute(
            sa.text(
                "INSERT INTO match (id, tournament_id, external_id, home_team_id, away_team_id, match_date, "
                "time_status, status, updated_at) VALUES (7, 1, 'g', 1, 1, '2026-10-24', 'time_set', 'scheduled', '2026-10-19')"
            )
        )
        for kind, key in [
            ("digest24", "7"),
            ("digest2", "7"),
            ("remind30", "7"),
            ("no_time", "7:2026-10-21"),
            ("poll_try", "7:2026-10-21T10"),
        ]:
            c.execute(
                sa.text(
                    "INSERT INTO notification_log (tenant_id, kind, dedup_key, match_id, sent_at) "
                    "VALUES (1, :k, :d, 7, '2026-10-19')"
                ),
                {"k": kind, "d": key},
            )
    eng.dispose()

    command.upgrade(cfg, "head")
    eng = sa.create_engine(f"sqlite:///{path}")
    with eng.connect() as c:
        keys = dict(c.execute(sa.text("SELECT kind, dedup_key FROM notification_log")).all())
    eng.dispose()
    cycle = f"7:{date(2026, 10, 24).isoformat()}"
    assert keys["digest24"] == keys["digest2"] == keys["remind30"] == cycle
    assert (
        keys["no_time"] == "7:2026-10-21" and keys["poll_try"] == "7:2026-10-21T10"
    )  # другие виды не трогаем
