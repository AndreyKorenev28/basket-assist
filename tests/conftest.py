from pathlib import Path

import pytest

from basket_assist.config import load_tenant_config
from basket_assist.storage import db

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
