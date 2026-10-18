from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select

from basket_assist.domain.models import Role
from basket_assist.storage.bootstrap import apply_tenant_config
from basket_assist.storage.tables import Base, Chat, League, Player, TeamEntry


async def test_migrations_match_models(session):
    """Миграции и модели не разошлись: autogenerate не видит различий."""

    def diff(sync_conn):
        return compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)

    conn = await session.connection()
    assert await conn.run_sync(diff) == []


async def test_bootstrap_is_idempotent(session, tenant_cfg):
    await apply_tenant_config(session, tenant_cfg)
    await apply_tenant_config(session, tenant_cfg)

    assert await session.scalar(select(func.count()).select_from(League)) == 2
    assert await session.scalar(select(func.count()).select_from(TeamEntry)) == 2
    assert await session.scalar(select(func.count()).select_from(Chat)) == 1
    roles = sorted((await session.scalars(select(Player.role))).all())
    assert roles == sorted([Role.CAPTAIN, Role.VICE_CAPTAIN])


async def test_league_settings_from_config(session, tenant_cfg):
    await apply_tenant_config(session, tenant_cfg)
    mlbl = (await session.execute(select(League).filter_by(key="mlbl"))).scalar_one()
    assert (mlbl.platform, mlbl.min_players, mlbl.max_players, mlbl.poll_weekday, mlbl.poll_time) == (
        "infobasket",
        5,
        12,
        2,
        "10:00",
    )
    assert mlbl.store_birth_date is False
    assert mlbl.default_venue_address is None  # пустая строка в конфиге = не задано
