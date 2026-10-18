"""Синхронизация данных лиг: матчи, заявки на сезон, таблицы. Обнаруживает изменения (change_event).

Первая синхронизация выступления — «начальная загрузка»: изменения не порождаются,
иначе каждый матч и каждый игрок выглядели бы как новость.
"""

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.domain.models import ChangeKind, MatchStatus, TimeStatus
from basket_assist.sources.base import (
    MatchDTO,
    PlatformAdapter,
    RosterEntryDTO,
    StandingDTO,
    TeamDTO,
    TeamRef,
)
from basket_assist.storage.tables import (
    ChangeEvent,
    League,
    LeagueTeam,
    Match,
    SeasonRosterEntry,
    Standing,
    TeamEntry,
    Tournament,
    utcnow,
)

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    matches_new: int = 0
    matches_updated: int = 0
    roster_size: int = 0
    standings: int = 0
    changes: list[str] = field(default_factory=list)


async def team_ref(session: AsyncSession, entry: TeamEntry) -> tuple[League, TeamRef]:
    team = await session.get(LeagueTeam, entry.league_team_id)
    tournament = await session.get(Tournament, entry.tournament_id)
    league = await session.get(League, team.league_id)
    return league, TeamRef(
        base_url=league.base_url,
        team_external_id=team.external_id,
        tournament_external_id=tournament.external_id,
        season_external_id=tournament.season,
    )


async def _league_team(session: AsyncSession, league_id: int, dto: TeamDTO) -> LeagueTeam:
    team = (
        await session.execute(select(LeagueTeam).filter_by(league_id=league_id, external_id=dto.external_id))
    ).scalar_one_or_none()
    if team is None:
        team = LeagueTeam(league_id=league_id, external_id=dto.external_id, name=dto.name)
        session.add(team)
        await session.flush()
    elif dto.name and team.name != dto.name:
        team.name = dto.name
    return team


def _change(session: AsyncSession, result: SyncResult, kind: ChangeKind, payload: dict, **refs) -> None:
    session.add(ChangeEvent(kind=kind, payload=json.dumps(payload, ensure_ascii=False, default=str), **refs))
    result.changes.append(f"{kind}: {payload}")


async def _sync_matches(
    session: AsyncSession, league: League, tournament_id: int, dtos: list[MatchDTO], result: SyncResult
) -> None:
    existing = {
        m.external_id: m
        for m in (await session.scalars(select(Match).filter_by(tournament_id=tournament_id))).all()
    }
    initial = not existing
    for dto in dtos:
        home = await _league_team(session, league.id, dto.home)
        away = await _league_team(session, league.id, dto.away)
        m = existing.get(dto.external_id)
        if m is None:
            m = Match(
                tournament_id=tournament_id,
                external_id=dto.external_id,
                home_team_id=home.id,
                away_team_id=away.id,
                match_date=dto.match_date,
                start_at=dto.start_at,
                time_status=TimeStatus.TIME_SET if dto.start_at else TimeStatus.DATE_ONLY,
                status=MatchStatus.PLAYED if dto.played else MatchStatus.SCHEDULED,
            )
            session.add(m)
            await session.flush()
            result.matches_new += 1
            if not initial:
                _change(
                    session,
                    result,
                    ChangeKind.MATCH_ADDED,
                    {
                        "date": dto.match_date,
                        "start_at": dto.start_at,
                        "vs": f"{dto.home.name} — {dto.away.name}",
                    },
                    match_id=m.id,
                )
        else:
            changed = False
            if m.match_date != dto.match_date:
                _change(
                    session,
                    result,
                    ChangeKind.RESCHEDULED,
                    {"was": m.match_date, "now": dto.match_date, "start_at": dto.start_at},
                    match_id=m.id,
                )
                m.match_date, m.start_at = dto.match_date, dto.start_at
                m.status = MatchStatus.RESCHEDULED
                m.time_status = TimeStatus.TIME_SET if dto.start_at else TimeStatus.DATE_ONLY
                m.time_source = None  # перенос даты: введённое вручную время больше не относится к матчу
                changed = True
            elif m.time_source == "manual" and dto.start_at is None:
                pass  # на сайте времени ещё нет — оставляем время, введённое руководителем
            elif m.time_source == "manual" and dto.start_at == m.start_at:
                m.time_source = None  # лига подтвердила то же время — дальше источник сайт
            elif m.start_at != dto.start_at:
                m.time_source = None  # время с сайта главнее ручного
                kind = ChangeKind.TIME_SET if m.start_at is None else ChangeKind.TIME_CHANGED
                _change(session, result, kind, {"was": m.start_at, "now": dto.start_at}, match_id=m.id)
                m.time_status = (
                    TimeStatus.DATE_ONLY
                    if dto.start_at is None
                    else (TimeStatus.TIME_SET if kind == ChangeKind.TIME_SET else TimeStatus.TIME_CHANGED)
                )
                m.start_at = dto.start_at
                changed = True
            if dto.played and m.status != MatchStatus.PLAYED:
                m.status = MatchStatus.PLAYED
                changed = True
            if changed:
                result.matches_updated += 1
        m.home_team_id, m.away_team_id = home.id, away.id
        m.venue_name = dto.venue_name
        m.venue_address = dto.venue_address or league.default_venue_address
        m.score_home, m.score_away = dto.score_home, dto.score_away


async def _sync_roster(
    session: AsyncSession, entry: TeamEntry, dtos: list[RosterEntryDTO], result: SyncResult
) -> None:
    rows = {
        r.external_person_id: r
        for r in (
            await session.scalars(
                select(SeasonRosterEntry).filter_by(
                    league_team_id=entry.league_team_id, tournament_id=entry.tournament_id
                )
            )
        ).all()
    }
    initial = not rows
    seen = set()
    for dto in dtos:
        seen.add(dto.external_person_id)
        row = rows.get(dto.external_person_id)
        fields = {
            "last_name": dto.last_name,
            "first_name": dto.first_name,
            "middle_name": dto.middle_name,
            "position": dto.position,
            "birth_date": dto.birth_date,
            "active": True,
        }
        if dto.number:  # номер из заявки на сезон; если его нет — не затираем номер из протокола
            fields |= {"number": dto.number, "number_source": "roster"}
        if row is None:
            session.add(
                SeasonRosterEntry(
                    league_team_id=entry.league_team_id,
                    tournament_id=entry.tournament_id,
                    external_person_id=dto.external_person_id,
                    **fields,
                )
            )
            if not initial:
                _change(
                    session,
                    result,
                    ChangeKind.ROSTER_ADDED,
                    {"person": dto.external_person_id},
                    league_team_id=entry.league_team_id,
                )
        else:
            if not row.active:
                _change(
                    session,
                    result,
                    ChangeKind.ROSTER_ADDED,
                    {"person": dto.external_person_id},
                    league_team_id=entry.league_team_id,
                )
            for k, v in fields.items():
                setattr(row, k, v)
    for pid, row in rows.items():
        if pid not in seen and row.active:
            row.active = False
            _change(
                session,
                result,
                ChangeKind.ROSTER_REMOVED,
                {"person": pid},
                league_team_id=entry.league_team_id,
            )
    result.roster_size = len(dtos)


async def _sync_standings(
    session: AsyncSession, league: League, tournament_id: int, dtos: list[StandingDTO], result: SyncResult
) -> None:
    for dto in dtos:
        team = await _league_team(session, league.id, dto.team)
        row = (
            await session.execute(
                select(Standing).filter_by(tournament_id=tournament_id, league_team_id=team.id)
            )
        ).scalar_one_or_none()
        if row is None:
            row = Standing(tournament_id=tournament_id, league_team_id=team.id)
            session.add(row)
        row.place, row.wins, row.losses = dto.place, dto.wins, dto.losses
        row.points_for, row.points_against = dto.points_for, dto.points_against
        row.updated_at = utcnow()
    result.standings = len(dtos)


PROTOCOLS_TO_READ = 3


async def _fill_numbers_from_protocols(
    session: AsyncSession, entry: TeamEntry, adapter: PlatformAdapter, ref: TeamRef
) -> None:
    """Если номеров в заявке на сезон нет (МЛБЛ), взять их из протоколов последних сыгранных матчей."""
    if "number" in adapter.roster_fields or not hasattr(adapter, "fetch_game_numbers"):
        return
    missing = (
        await session.scalars(
            select(SeasonRosterEntry).filter_by(
                league_team_id=entry.league_team_id, tournament_id=entry.tournament_id, active=True
            )
        )
    ).all()
    missing = [r for r in missing if r.number is None or r.number_source == "protocol"]
    if not missing:
        return
    played = (
        await session.scalars(
            select(Match.external_id)
            .where(Match.tournament_id == entry.tournament_id, Match.status == MatchStatus.PLAYED)
            .where(
                or_(Match.home_team_id == entry.league_team_id, Match.away_team_id == entry.league_team_id)
            )
            .order_by(Match.match_date.desc())
            .limit(PROTOCOLS_TO_READ)
        )
    ).all()
    if not played:
        return
    numbers = await adapter.fetch_game_numbers(ref, list(played))
    for row in missing:
        if number := numbers.get(row.external_person_id):
            row.number, row.number_source = number, "protocol"


async def sync_team_entry(
    session: AsyncSession,
    entry: TeamEntry,
    adapter: PlatformAdapter,
    *,
    matches: bool = True,
    roster: bool = True,
    standings: bool = True,
) -> SyncResult:
    """Синхронизировать одно выступление. Коммитит сессию."""
    league, ref = await team_ref(session, entry)
    result = SyncResult()
    if matches:
        await _sync_matches(session, league, entry.tournament_id, await adapter.fetch_matches(ref), result)
    if roster:
        await _sync_roster(session, entry, await adapter.fetch_roster(ref), result)
        await session.flush()
        await _fill_numbers_from_protocols(session, entry, adapter, ref)
    if standings:
        await _sync_standings(
            session, league, entry.tournament_id, await adapter.fetch_standings(ref), result
        )
    await session.commit()
    for c in result.changes:
        log.info("[%s] изменение: %s", league.key, c)
    return result
