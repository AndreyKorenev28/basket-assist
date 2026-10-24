import shutil
from datetime import UTC
from pathlib import Path

import pytest
from sqlalchemy import event

from basket_assist.config import load_tenant_config
from basket_assist.services import polls, weekly
from basket_assist.storage import db
from basket_assist.storage.tables import PollMessage

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_TENANT = ROOT / "config" / "tenant.example.toml"


@pytest.fixture(scope="session")
def migrated_db(tmp_path_factory) -> Path:
    """База со всеми миграциями — один раз на прогон (миграции на каждый тест занимали ~2/3 времени)."""
    path = tmp_path_factory.mktemp("template") / "template.db"
    db.upgrade(f"sqlite+aiosqlite:///{path.as_posix()}")
    return path


@pytest.fixture
def db_url(tmp_path: Path, migrated_db: Path) -> str:
    """Своя база на каждый тест — копия готовой (изоляция та же, без повторных миграций)."""
    path = tmp_path / "test.db"
    shutil.copyfile(migrated_db, path)
    return f"sqlite+aiosqlite:///{path.as_posix()}"


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
        # ADR 0008: анонс скрывает игры через 2 ч после начала по polls.utcnow — на время tick часы polls
        # тоже идут по now теста, иначе к дню запуска тестов игры недели 21–25.10.2026 уже «сыграны»
        clock[0] = now
        prev = polls.utcnow
        if now is not None:
            polls.utcnow = lambda: now.astimezone(UTC).replace(tzinfo=None)
        try:
            return await tick(session, channel, now)
        finally:
            clock[0] = None
            polls.utcnow = prev

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


@pytest.fixture(autouse=True)
def reset_service_admin():
    """Администратор сервиса (notify.set_admin) — состояние процесса: между тестами не переносится."""
    from basket_assist.services import notify

    notify.set_admin(None)
    yield
    notify.set_admin(None)
