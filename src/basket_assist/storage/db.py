"""Подключение к БД и применение миграций."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def make_engine(db_url: str) -> AsyncEngine:
    return create_async_engine(db_url)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def alembic_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def upgrade(db_url: str, db_path: Path | None = None) -> None:
    """Применить миграции до последней версии (синхронно, при старте)."""
    if db_path is not None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_url), "head")
