"""Синхронизация: начальная загрузка без «новостей», затем обнаружение изменений."""

from dataclasses import replace
from datetime import date, datetime

from sqlalchemy import select

from basket_assist.domain.models import ChangeKind, MatchStatus, TimeStatus
from basket_assist.services.sync import sync_team_entry
from basket_assist.sources.base import MatchDTO, RosterEntryDTO, StandingDTO, TeamDTO
from basket_assist.storage.bootstrap import apply_tenant_config
from basket_assist.storage.tables import ChangeEvent, League, LeagueTeam, Match, SeasonRosterEntry, TeamEntry

US = TeamDTO("279", "Westchester")
THEM = TeamDTO("2301", "Pokrasote")


class FakeAdapter:
    platform = "mtgame"
    roster_fields = frozenset({"number"})

    def __init__(self):
        self.matches = [
            MatchDTO(
                "1",
                US,
                THEM,
                date(2026, 10, 24),
                datetime(2026, 10, 24, 9, 0),
                played=False,
                venue_name="Зал",
            ),
        ]
        self.roster = [
            RosterEntryDTO("p1", "Петров", "Иван", number="7"),
            RosterEntryDTO("p2", "Сидоров", "Олег"),
        ]

    async def fetch_matches(self, team):
        return self.matches

    async def fetch_roster(self, team):
        return self.roster

    async def fetch_standings(self, team):
        return [StandingDTO(US, 28, 2, 5), StandingDTO(THEM, 3, 6, 1)]


async def _abl_entry(session, tenant_cfg):
    await apply_tenant_config(session, tenant_cfg)
    league = (await session.execute(select(League).filter_by(key="abl"))).scalar_one()
    team = (
        await session.execute(select(LeagueTeam).filter_by(league_id=league.id, external_id="279"))
    ).scalar_one()
    return (await session.execute(select(TeamEntry).filter_by(league_team_id=team.id))).scalar_one()


async def _kinds(session):
    return [e.kind for e in (await session.scalars(select(ChangeEvent).order_by(ChangeEvent.id))).all()]


async def test_initial_sync_creates_data_without_changes(session, tenant_cfg):
    entry = await _abl_entry(session, tenant_cfg)
    r = await sync_team_entry(session, entry, FakeAdapter())
    assert (r.matches_new, r.roster_size, r.standings) == (1, 2, 2)
    assert await _kinds(session) == []
    m = (await session.scalars(select(Match))).one()
    assert m.time_status == TimeStatus.TIME_SET and m.status == MatchStatus.SCHEDULED
    assert m.venue_address is None


async def test_detects_time_change_reschedule_new_match_and_roster(session, tenant_cfg):
    entry = await _abl_entry(session, tenant_cfg)
    src = FakeAdapter()
    await sync_team_entry(session, entry, src)

    # время сдвинули в тот же день
    src.matches = [replace(src.matches[0], start_at=datetime(2026, 10, 24, 11, 20))]
    await sync_team_entry(session, entry, src)
    m = (await session.scalars(select(Match))).one()
    assert m.time_status == TimeStatus.TIME_CHANGED

    # перенесли на другую дату + появился матч следующего тура + дозаявка и отзаявка
    src.matches = [
        replace(src.matches[0], match_date=date(2026, 10, 25), start_at=datetime(2026, 10, 25, 9, 0)),
        MatchDTO("2", THEM, US, date(2026, 10, 31), datetime(2026, 10, 31, 12, 0), played=False),
    ]
    src.roster = [src.roster[0], RosterEntryDTO("p3", "Новиков", "Пётр")]
    await sync_team_entry(session, entry, src)

    assert await _kinds(session) == [
        ChangeKind.TIME_CHANGED,
        ChangeKind.RESCHEDULED,
        ChangeKind.MATCH_ADDED,
        ChangeKind.ROSTER_ADDED,
        ChangeKind.ROSTER_REMOVED,
    ]
    await session.refresh(m)
    assert m.status == MatchStatus.RESCHEDULED and m.match_date == date(2026, 10, 25)
    inactive = (await session.scalars(select(SeasonRosterEntry).filter_by(active=False))).all()
    assert [r.external_person_id for r in inactive] == ["p2"]


async def test_repeated_sync_without_changes_is_quiet(session, tenant_cfg):
    entry = await _abl_entry(session, tenant_cfg)
    src = FakeAdapter()
    await sync_team_entry(session, entry, src)
    r = await sync_team_entry(session, entry, src)
    assert (r.matches_new, r.matches_updated, r.changes) == (0, 0, [])
