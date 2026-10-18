"""Адаптер платформы Infobasket (МЛБЛ, *.ilovebasket.ru). Описание API — notes/sources/ilovebasket/README.md.

Особенности:
- даты как "/Date(ms)/" — UTC в миллисекундах; HasTime — время назначено;
- TeamGames и TeamRoster берут ID сезона (compId), таблица — ID этапа;
- номеров в заявке на сезон нет (они только в протоколах матчей) — number = None;
- ответы иногда приходят с BOM.
"""

import json
import re
from datetime import UTC, datetime
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

API = "https://reg.infobasket.su"
TZ = ZoneInfo("Europe/Moscow")
_MS_DATE = re.compile(r"/Date\((-?\d+)\)/")


def parse_ms_date(value: str | None) -> datetime | None:
    m = _MS_DATE.fullmatch(value or "")
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000, UTC).replace(tzinfo=None)


def _name(g: dict, side: str) -> str:
    return g.get(f"CompTeamName{side}ru") or g.get(f"ShortTeamName{side}ru") or g.get(f"TeamName{side}ru")


def parse_games(items: list[dict]) -> list[MatchDTO]:
    out = []
    for g in items:
        try:
            match_date = datetime.strptime(g["GameDate"], "%d.%m.%Y").date()
        except (KeyError, ValueError) as e:
            raise SourceError("infobasket", "нет даты матча", {"GameID": g.get("GameID")}) from e
        start = parse_ms_date(g.get("GameDateTime")) if g.get("HasTime") else None
        played = g.get("GameStatus") == 1
        out.append(
            MatchDTO(
                external_id=str(g["GameID"]),
                home=TeamDTO(external_id=str(g["TeamAid"]), name=_name(g, "A")),
                away=TeamDTO(external_id=str(g["TeamBid"]), name=_name(g, "B")),
                match_date=match_date,
                start_at=start,
                played=played,
                venue_name=g.get("ArenaRu") or None,
                venue_address=None,  # Infobasket адресов не отдаёт
                score_home=g.get("ScoreA") if played else None,
                score_away=g.get("ScoreB") if played else None,
            )
        )
    return out


def parse_roster(data: dict, with_birth_date: bool) -> list[RosterEntryDTO]:
    players = data.get("Players")
    if not isinstance(players, list):
        raise SourceError("infobasket", "в заявке на сезон нет списка Players")
    out = []
    for p in players:
        if p.get("IsActive") is False:
            continue
        info = p.get("PersonInfo") or {}
        last, first = info.get("PersonLastNameRu"), info.get("PersonFirstNameRu")
        if not (last and first):
            raise SourceError("infobasket", "в заявке на сезон нет ФИО", {"PersonID": p.get("PersonID")})
        birth = p.get("PersonBirth") or info.get("PersonBirth")
        out.append(
            RosterEntryDTO(
                external_person_id=str(p["PersonID"]),
                last_name=last.strip(),
                first_name=first.strip(),
                middle_name=(info.get("PersonSecondNameRu") or None),
                number=(p.get("DisplayNumber") or None),
                position=p.get("Position") or None,
                birth_date=(
                    datetime.strptime(birth, "%d.%m.%Y").date() if with_birth_date and birth else None
                ),
            )
        )
    return out


def parse_standings(items: list[dict]) -> list[StandingDTO]:
    out = []
    for t in items:
        s = t.get("Standings") or {}
        names = t.get("CompTeamName") or {}
        out.append(
            StandingDTO(
                team=TeamDTO(
                    external_id=str(t["TeamID"]),
                    name=names.get("CompTeamNameRu") or names.get("CompTeamShortNameRu") or str(t["TeamID"]),
                ),
                place=t.get("Place") or 0,
                wins=s.get("StandingWin") or 0,
                losses=s.get("StandingLose") or 0,
                points_for=s.get("StandingGoalPlus"),
                points_against=s.get("StandingGoalMinus"),
            )
        )
    return sorted(out, key=lambda x: x.place)


class InfobasketAdapter:
    platform = "infobasket"
    roster_fields = frozenset({"position", "birth_date"})

    def __init__(self, client: httpx.AsyncClient, store_birth_date: bool = False):
        self._client = client
        self._store_birth_date = store_birth_date

    async def _get(self, path: str, **params) -> list | dict:
        r = await self._client.get(f"{API}{path}", params={"format": "json", **params})
        if r.status_code != 200:
            raise SourceError("infobasket", f"HTTP {r.status_code} для {path}")
        return json.loads(r.content.decode("utf-8-sig"))

    def _season(self, team: TeamRef) -> str:
        if not team.season_external_id:
            raise SourceError("infobasket", "не задан ID сезона (season_external_id)")
        return team.season_external_id

    async def fetch_matches(self, team: TeamRef) -> list[MatchDTO]:
        items = await self._get(f"/Widget/TeamGames/{team.team_external_id}", compId=self._season(team))
        if not isinstance(items, list):
            raise SourceError("infobasket", "список игр — не массив")
        return parse_games(items)

    async def fetch_roster(self, team: TeamRef) -> list[RosterEntryDTO]:
        data = await self._get(
            f"/Widget/TeamRoster/{team.team_external_id}", compId=self._season(team), lang="ru"
        )
        if not isinstance(data, dict):
            raise SourceError("infobasket", "заявка на сезон — не объект")
        return parse_roster(data, self._store_birth_date)

    async def fetch_standings(self, team: TeamRef) -> list[StandingDTO]:
        items = await self._get(f"/Widget/CompTeamResults/{team.tournament_external_id}")
        if not isinstance(items, list):
            raise SourceError("infobasket", "таблица — не массив")
        return parse_standings(items)
