from datetime import UTC
from pathlib import Path

import pytest
from sqlalchemy import event

from basket_assist.config import load_tenant_config
from basket_assist.services import weekly
from basket_assist.storage import db
from basket_assist.storage.tables import PollMessage

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_TENANT = ROOT / "config" / "tenant.example.toml"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    return url


@pytest.fixture
async def session(db_url: str):
    engine = db.make_engine(db_url)
    async with db.make_sessionmaker(engine)() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def tenant_cfg():
    return load_tenant_config(EXAMPLE_TENANT)


@pytest.fixture(autouse=True)
def poll_posted_at_follows_tick(monkeypatch):
    """Анонс, опубликованный из weekly.tick(now=...), получает posted_at = now теста, а не настоящее время.

    В бою tick идёт по настоящим часам и они совпадают; в тестах «сейчас» — фиксированные даты 2026 г.,
    и без этого напоминание (posted_at + 3 ч) зависело бы от дня, в который запущены тесты.
    """
    clock: list = [None]
    tick = weekly.tick

    async def tick_with_clock(session, channel, now=None):
        clock[0] = now
        try:
            return await tick(session, channel, now)
        finally:
            clock[0] = None

    def stamp(_mapper, _conn, target):
        if clock[0] is not None and target.posted_at is None:
            target.posted_at = clock[0].astimezone(UTC).replace(tzinfo=None)

    monkeypatch.setattr(weekly, "tick", tick_with_clock)
    event.listen(PollMessage, "before_insert", stamp)
    yield
    event.remove(PollMessage, "before_insert", stamp)


@pytest.fixture(autouse=True)
def reset_games_pause():
    """Пауза на «игры» в беседе (handlers._last_games) — состояние процесса: между тестами не переносится."""
    from basket_assist.bot import handlers

    handlers._last_games.clear()
    yield
    handlers._last_games.clear()
