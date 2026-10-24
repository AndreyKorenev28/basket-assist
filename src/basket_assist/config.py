"""Конфигурация: секреты из .env, команда и выступления — из config/tenant.toml (вне git)."""

import tomllib
from datetime import time
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from basket_assist.domain.models import Platform


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    vk_group_token: SecretStr | None = None
    vk_group_id: int | None = None
    # администратор сервиса — технические сигналы в личку (пропали кнопки анонса, отметки текстом)
    admin_vk_user_id: int | None = None
    db_path: Path = Path("data/basket_assist.db")
    tenant_config: Path = Path("config/tenant.toml")
    log_level: str = "INFO"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"


class LeagueConfig(BaseModel):
    """Лига и наше выступление в ней (в MVP — одно выступление на лигу)."""

    key: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    platform: Platform
    base_url: str
    timezone: str = "Europe/Moscow"
    min_players: int = 5
    max_players: int = 12
    poll_weekday: int = Field(2, ge=0, le=6, description="0 — понедельник; по умолчанию среда")
    poll_time: time = time(10, 0)
    store_birth_date: bool = False
    default_venue_address: str | None = None
    # выступление
    team_name: str
    team_url: str
    team_external_id: str
    tournament_external_id: str
    season_external_id: str | None = None


class PersonConfig(BaseModel):
    vk_user_id: int
    display_name: str


class TenantConfig(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    vk_chat_peer_id: int | None = None
    captain: PersonConfig | None = None
    vice_captains: list[PersonConfig] = []
    leagues: list[LeagueConfig]


def load_tenant_config(path: Path) -> TenantConfig:
    with path.open("rb") as f:
        return TenantConfig.model_validate(tomllib.load(f))
