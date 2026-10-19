"""Интерфейс адаптера платформы лиги и DTO, общие для всех платформ."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

import httpx

USER_AGENT = "basket-assist/0.1 (+https://vk.com/club242389357)"


@dataclass(frozen=True)
class TeamRef:
    """Наше выступление глазами сайта лиги."""

    base_url: str
    team_external_id: str
    tournament_external_id: str
    season_external_id: str | None = None


@dataclass(frozen=True)
class TeamDTO:
    external_id: str
    name: str


@dataclass(frozen=True)
class MatchDTO:
    external_id: str
    home: TeamDTO
    away: TeamDTO
    match_date: date  # дата по таймзоне лиги
    start_at: datetime | None  # UTC без tzinfo; None — время не назначено
    played: bool
    venue_name: str | None = None
    venue_address: str | None = None
    score_home: int | None = None
    score_away: int | None = None
    # дата не опубликована, match_date — ожидаемая (суббота ближайших выходных); АБЛ: черновик тура
    date_is_estimate: bool = False


@dataclass(frozen=True)
class RosterEntryDTO:
    external_person_id: str
    last_name: str
    first_name: str
    middle_name: str | None = None
    number: str | None = None
    position: str | None = None
    birth_date: date | None = None


@dataclass(frozen=True)
class StandingDTO:
    team: TeamDTO
    place: int
    wins: int
    losses: int
    points_for: int | None = None
    points_against: int | None = None


@dataclass
class SourceError(Exception):
    """Ответ сайта лиги не похож на ожидаемый — падаем громко (CLAUDE.md)."""

    platform: str
    detail: str
    extra: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.platform}] {self.detail}"


class PlatformAdapter(Protocol):
    platform: str
    roster_fields: frozenset[str]  # какие поля заявки на сезон отдаёт: number, position, birth_date

    async def fetch_matches(self, team: TeamRef) -> list[MatchDTO]: ...
    async def fetch_roster(self, team: TeamRef) -> list[RosterEntryDTO]: ...
    async def fetch_standings(self, team: TeamRef) -> list[StandingDTO]: ...


def make_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
