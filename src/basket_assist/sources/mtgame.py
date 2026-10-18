"""Адаптер платформы MTGame (АБЛ, ablforpeople.com). Описание API — notes/sources/abl/README.md.

Особенности:
- команда в лиге идентифицируется team.id (сквозной ID), а заявка на сезон — tournament_team.id;
- матч появляется в API, только когда тур составлен, и сразу с временем (datetime в UTC);
- название команды в турнире (tournament_team.name) точнее, чем team.name.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx

from basket_assist.sources.base import (
    MatchDTO,
    RosterEntryDTO,
    SourceError,
    StandingDTO,
    TeamDTO,
    TeamRef,
)

API = "https://mtgame.ru/api/v1"
TZ = ZoneInfo("Europe/Moscow")

POSITIONS = {
    "point_guard": "Разыгрывающий защитник",
    "shooting_guard": "Атакующий защитник",
    "small_forward": "Лёгкий форвард",
    "power_forward": "Тяжёлый форвард",
    "center": "Центровой",
}


def _team(team: dict | None, tournament_team: dict | None) -> TeamDTO:
    if not team or "id" not in team:
        raise SourceError("mtgame", "в игре нет команды", {"team": team})
    name = (tournament_team or {}).get("name") or team.get("name") or str(team["id"])
    return TeamDTO(external_id=str(team["id"]), name=name)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)


def parse_games(items: list[dict]) -> list[MatchDTO]:
    out = []
    for g in items:
        start = _parse_dt(g.get("datetime"))
        court = g.get("tournament_court") or {}
        played = g.get("status") == "closed"
        out.append(
            MatchDTO(
                external_id=str(g["id"]),
                home=_team(g.get("team"), g.get("tournament_team")),
                away=_team(g.get("competitor_team"), g.get("competitor_tournament_team")),
                match_date=(start.replace(tzinfo=UTC).astimezone(TZ).date() if start else date.min),
                start_at=start,
                played=played,
                venue_name=court.get("name"),
                venue_address=court.get("address") or g.get("location"),
                score_home=g.get("team_score") if played else None,
                score_away=g.get("competitor_team_score") if played else None,
            )
        )
    return out


def parse_standings(items: list[dict]) -> list[StandingDTO]:
    # API отдаёт таблицу уже отсортированной по месту
    return [
        StandingDTO(
            team=TeamDTO(external_id=str(t["team_id"]), name=t.get("name") or str(t["team_id"])),
            place=i,
            wins=t.get("won_games_count") or 0,
            losses=t.get("lose_games_count") or 0,
            points_for=t.get("score_sum"),
            points_against=t.get("missed_sum"),
        )
        for i, t in enumerate(items, start=1)
    ]


def parse_roster(items: list[dict], with_birth_date: bool) -> list[RosterEntryDTO]:
    out = []
    for r in items:
        if not r.get("active", True):
            continue
        tu = r.get("team_user") or {}
        user = tu.get("user") or {}
        lp = r.get("league_player") or {}
        last = user.get("last_name") or lp.get("last_name")
        first = user.get("first_name") or lp.get("first_name")
        if not (last and first):
            raise SourceError("mtgame", "в заявке на сезон нет ФИО", {"id": r.get("id")})
        position = (user.get("basketball_profile") or {}).get("position")
        birth = user.get("birth_date") or lp.get("birth_date")
        out.append(
            RosterEntryDTO(
                external_person_id=str(lp.get("id") or user.get("id") or r["id"]),
                last_name=last.strip(),
                first_name=first.strip(),
                middle_name=(user.get("middle_name") or lp.get("middle_name") or None),
                number=(str(tu["number"]) if tu.get("number") not in (None, "") else None),
                position=POSITIONS.get(position, position),
                birth_date=date.fromisoformat(birth) if with_birth_date and birth else None,
            )
        )
    return out


class MtgameAdapter:
    platform = "mtgame"
    roster_fields = frozenset({"number", "position", "birth_date"})

    def __init__(self, client: httpx.AsyncClient, store_birth_date: bool = False):
        self._client = client
        self._store_birth_date = store_birth_date
        self._tournament_team_ids: dict[tuple[str, str], str] = {}

    async def _get(self, path: str, **params) -> list | dict:
        r = await self._client.get(f"{API}{path}", params=params or None)
        if r.status_code != 200:
            raise SourceError("mtgame", f"HTTP {r.status_code} для {path}")
        return r.json()

    async def fetch_matches(self, team: TeamRef) -> list[MatchDTO]:
        items = await self._get(
            f"/tournament/{team.tournament_external_id}/games/",
            page=1,
            size=999,
            team_id=team.team_external_id,
        )
        if not isinstance(items, list):
            raise SourceError("mtgame", "список игр — не массив")
        return parse_games(items)

    async def _teams(self, team: TeamRef) -> list[dict]:
        items = await self._get(f"/tournament/{team.tournament_external_id}/teams/")
        if not isinstance(items, list):
            raise SourceError("mtgame", "таблица турнира — не массив")
        for t in items:
            self._tournament_team_ids[(team.tournament_external_id, str(t["team_id"]))] = str(t["id"])
        return items

    async def fetch_standings(self, team: TeamRef) -> list[StandingDTO]:
        return parse_standings(await self._teams(team))

    async def fetch_roster(self, team: TeamRef) -> list[RosterEntryDTO]:
        key = (team.tournament_external_id, team.team_external_id)
        if key not in self._tournament_team_ids:
            await self._teams(team)
        tt_id = self._tournament_team_ids.get(key)
        if tt_id is None:
            raise SourceError("mtgame", "команды нет в турнире", {"team": team.team_external_id})
        items = await self._get(f"/tournament_team/{tt_id}/users/")
        if not isinstance(items, list):
            raise SourceError("mtgame", "заявка на сезон — не массив")
        return parse_roster(items, self._store_birth_date)
