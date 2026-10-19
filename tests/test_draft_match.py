"""АБЛ публикует игру нового тура черновиком: status=draft, datetime=null, без зала (data-sources.md).
Дата такой игры — ожидаемая (суббота ближайших выходных, Match.date_is_estimate); публикация даты и времени —
«время назначено», а не перенос (domain.md, «Жизненный цикл матча», вариант B). Вымышленные имена."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import select

from basket_assist.bot import leader
from basket_assist.channels.base import ButtonPress, MessageRef
from basket_assist.domain.models import ChangeKind, MatchStatus, TimeStatus
from basket_assist.services import scheduler, weekly
from basket_assist.services.schedule import format_when, upcoming_matches
from basket_assist.services.sync import sync_team_entry
from basket_assist.sources import mtgame
from basket_assist.storage.db import alembic_config
from basket_assist.storage.tables import ChangeEvent, League, Match, PollMessage
from tests.fakes import FakeChannel
from tests.test_roster import CAPTAIN, CHAT, _player
from tests.test_sources import load
from tests.test_sync import US, FakeAdapter, _abl_entry, _kinds
from tests.test_weekly import FRI, NO_TIME, SAT, SUN, THU, WED, _ready, _texts, _to, _week_match, at

MON = date(2026, 10, 19)
NEXT_SAT = date(2026, 10, 31)
SITE_SUN = datetime(2026, 10, 25, 15, 30)  # 18:30 МСК в воскресенье
WEEKEND = "выходные 24–25.10, дата и время пока не назначены"


# ---------- парсинг черновика MTGame ----------


@pytest.mark.parametrize(
    "today, sat",
    [
        (MON, SAT),
        (date(2026, 10, 23), SAT),  # пятница
        (SAT, SAT),  # суббота — эти выходные
        (SUN, NEXT_SAT),  # воскресенье — следующие выходные
    ],
)
def test_mtgame_draft_date_is_expected_saturday(today, sat):
    [g] = mtgame.parse_games(load("mtgame/game_draft.json"), today=today)
    assert g.external_id == "164568"
    assert g.start_at is None and g.date_is_estimate is True
    assert g.match_date == sat and g.match_date.weekday() == 5
    assert g.played is False and g.venue_name is None
    assert "279" in (g.home.external_id, g.away.external_id)


def test_mtgame_game_with_datetime_is_not_estimate():
    games = mtgame.parse_games(load("mtgame/games_team.json"), today=MON)
    assert games and all(not g.date_is_estimate for g in games if g.start_at is not None)


# ---------- синхронизация ----------


def _draft_adapter(day: date = SAT) -> FakeAdapter:
    src = FakeAdapter()
    src.matches = [
        replace(src.matches[0], match_date=day, start_at=None, venue_name=None, date_is_estimate=True)
    ]
    return src


async def _synced_draft(session, tenant_cfg):
    entry = await _abl_entry(session, tenant_cfg)
    src = _draft_adapter()
    await sync_team_entry(session, entry, src)
    m = (await session.scalars(select(Match))).one()
    assert (m.match_date, m.date_is_estimate, m.start_at) == (SAT, True, None)
    assert m.time_status == TimeStatus.DATE_ONLY
    return entry, src, m


async def _with_new_draft(session, tenant_cfg):
    """Начальная загрузка (обычный матч), затем на сайте появляется черновик нового тура."""
    entry = await _abl_entry(session, tenant_cfg)
    src = FakeAdapter()
    await sync_team_entry(session, entry, src)  # начальная загрузка — без новостей
    rival = replace(src.matches[0].away, external_id="2302", name="Соперник Д")
    draft = replace(
        src.matches[0],
        external_id="2",
        home=rival,
        away=US,
        match_date=SAT,
        start_at=None,
        venue_name=None,
        date_is_estimate=True,
    )
    src.matches = [src.matches[0], draft]
    await sync_team_entry(session, entry, src)
    m = (await session.scalars(select(Match).filter_by(external_id="2"))).one()
    return entry, src, m


async def test_new_draft_is_silent(session, tenant_cfg):
    """Черновик никому не показывается: ни события, ни сообщения в беседу."""
    _, _, m = await _with_new_draft(session, tenant_cfg)
    assert (m.match_date, m.date_is_estimate) == (SAT, True)
    assert await _kinds(session) == []
    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    assert ch.sent == []


@pytest.mark.parametrize("start", [SITE_SUN, None])
async def test_draft_gets_real_date_is_match_added(session, tenant_cfg, start):
    """Лига опубликовала дату (с временем или без) — матч «появляется»: одно MATCH_ADDED, без RESCHEDULED/TIME_SET."""
    entry, src, m = await _with_new_draft(session, tenant_cfg)
    src.matches = [
        src.matches[0],
        replace(src.matches[1], match_date=SUN, start_at=start, venue_name="Зал", date_is_estimate=False),
    ]
    await sync_team_entry(session, entry, src)
    await session.refresh(m)
    assert await _kinds(session) == [ChangeKind.MATCH_ADDED]
    assert (m.match_date, m.start_at, m.date_is_estimate) == (SUN, start, False)
    assert m.status == MatchStatus.SCHEDULED and m.time_source is None
    assert m.time_status == (TimeStatus.TIME_SET if start else TimeStatus.DATE_ONLY)
    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    when = "вс 25.10, 18:30" if start else "вс 25.10"
    [(peer, text, _)] = ch.sent
    assert peer == CHAT and text.startswith(
        "📅 В календаре лиги новый матч: Соперник Д — Westchester (АБЛ) — вс 25.10"
    )
    assert when in text and "выходные" not in text and "01.01" not in text


async def test_change_event_for_draft_not_sent(session, tenant_cfg):
    _, _, m = await _with_new_draft(session, tenant_cfg)
    for kind in (ChangeKind.MATCH_ADDED, ChangeKind.TIME_SET, ChangeKind.RESCHEDULED):
        session.add(ChangeEvent(kind=kind, match_id=m.id))
    await session.commit()
    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    assert ch.sent == []


async def test_draft_moves_to_next_weekend_quietly(session, tenant_cfg):
    entry, src, m = await _synced_draft(session, tenant_cfg)
    src.matches = [replace(src.matches[0], match_date=NEXT_SAT)]
    r = await sync_team_entry(session, entry, src)
    await session.refresh(m)
    assert await _kinds(session) == [] and r.changes == []
    assert (m.match_date, m.date_is_estimate, m.status) == (NEXT_SAT, True, MatchStatus.SCHEDULED)


async def test_repeated_draft_sync_is_quiet(session, tenant_cfg):
    entry, src, m = await _synced_draft(session, tenant_cfg)
    r = await sync_team_entry(session, entry, src)
    assert (r.matches_updated, r.changes) == (0, [])


async def test_manual_sunday_time_survives_draft_sync(session, tenant_cfg):
    entry, src, m = await _synced_draft(session, tenant_cfg)
    text = await leader.set_manual_time(session, entry.tenant_id, m, "18:30", (25, 10))
    assert text.startswith("Время записано")
    await session.refresh(m)
    assert (m.match_date, m.start_at, m.date_is_estimate, m.time_source) == (SUN, SITE_SUN, False, "manual")

    await sync_team_entry(session, entry, src)  # на сайте всё ещё черновик (ожидаемая суббота)
    await session.refresh(m)
    assert (m.match_date, m.start_at, m.date_is_estimate, m.time_source) == (SUN, SITE_SUN, False, "manual")
    assert m.time_status == TimeStatus.TIME_SET
    assert await _kinds(session) == []

    # лига подтвердила тот же день и время — тихо, дальше источник сайт
    src.matches = [replace(src.matches[0], match_date=SUN, start_at=SITE_SUN, date_is_estimate=False)]
    await sync_team_entry(session, entry, src)
    await session.refresh(m)
    assert (m.match_date, m.start_at, m.time_source) == (SUN, SITE_SUN, None)
    assert await _kinds(session) == []


# ---------- format_when ----------


def test_format_when_expected_weekend():
    league = League(timezone="Europe/Moscow")
    m = Match(match_date=SAT, start_at=None, date_is_estimate=True)
    assert format_when(m, league) == WEEKEND
    # время введено/опубликовано — обычный формат
    m2 = Match(match_date=SUN, start_at=SITE_SUN, date_is_estimate=False)
    assert format_when(m2, league) == "вс 25.10, 18:30"


def test_format_when_expected_weekend_across_months():
    m = Match(match_date=NEXT_SAT, start_at=None, date_is_estimate=True)
    assert format_when(m, League(timezone="Europe/Moscow")).startswith("выходные 31.10–01.11")


# ---------- ручное время для ожидаемых выходных ----------


def _upcoming_sat() -> date:
    return mtgame.expected_weekend(datetime.now(UTC).date() + timedelta(days=1))


async def _estimate_match(session, entry, ext="g1"):
    m = await _week_match(session, entry, ext, day=_upcoming_sat(), hhmm=None)
    m.date_is_estimate = True
    await session.commit()
    return m


async def test_upcoming_matches_hides_draft_by_default(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _estimate_match(session, entries["abl"])
    assert [x[3].id for x in await upcoming_matches(session, tenant.id)] == []
    assert [x[3].id for x in await upcoming_matches(session, tenant.id, include_estimate=True)] == [m.id]


@pytest.mark.parametrize("offset", [0, 1])  # суббота или воскресенье
async def test_time_command_accepts_both_weekend_days(session, tenant_cfg, offset):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _estimate_match(session, entries["abl"])
    day = m.match_date + timedelta(days=offset)
    text, buttons = await leader.time_command(session, tenant.id, f" {day:%d.%m} 18:30")
    assert text.startswith("Время записано") and buttons == []
    await session.refresh(m)
    assert (m.match_date, m.date_is_estimate, m.time_source) == (day, False, "manual")


async def test_time_command_other_day_not_found_for_estimate(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _estimate_match(session, entries["abl"])
    fri = m.match_date - timedelta(days=1)
    text, _ = await leader.time_command(session, tenant.id, f" {fri:%d.%m} 18:30")
    assert text.startswith("Не нашёл нашего предстоящего матча")
    await session.refresh(m)
    assert m.date_is_estimate and m.start_at is None


async def test_tm_button_with_day_sets_sunday(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _estimate_match(session, entries["abl"])
    sun = m.match_date + timedelta(days=1)
    captain = await _player(session, tenant, CAPTAIN)
    ch = FakeChannel()
    press = ButtonPress(
        CAPTAIN,
        CAPTAIN,
        MessageRef(CAPTAIN, "500"),
        {"a": "tm", "m": m.id, "t": "18:30", "d": f"{sun.day}.{sun.month}"},
        "e",
    )
    text, _ = await leader.on_button(session, ch, tenant.id, captain, press)
    assert text.startswith("Время записано")
    await session.refresh(m)
    assert (m.match_date, m.date_is_estimate, m.time_source) == (sun, False, "manual")


# ---------- недельный цикл ----------


async def test_weekly_skips_draft_completely(session, tenant_cfg):
    """Ни опроса, ни «время не назначено», ни сводок для черновика."""
    _, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1", day=SAT, hhmm=None)
    m.date_is_estimate = True
    await session.commit()
    ch = FakeChannel()
    for now in (at(WED, 10), at(THU, 10), at(FRI, 10), at(SAT, 10)):
        await weekly.tick(session, ch, now=now)
    assert ch.sent == [] and ch.pins == []
    assert (await session.scalars(select(PollMessage))).all() == []


async def test_draft_with_manual_time_becomes_regular(session, tenant_cfg):
    """После «время 25.10 18:30» черновик — обычный матч: виден в «Играх», в среду запускается опрос."""
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1", day=SAT, hhmm=None)
    m.date_is_estimate = True
    await session.commit()
    await leader.set_manual_time(session, tenant.id, m, "18:30", (SUN.day, SUN.month))
    await session.refresh(m)
    assert (m.match_date, m.date_is_estimate, m.time_source) == (SUN, False, "manual")
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    [(poll_text, _)] = _to(ch, CHAT)
    assert "Соперник g1 — Westchester" in poll_text
    assert _texts(ch, CAPTAIN, NO_TIME) == []


# ---------- миграция 0005 ----------


def test_migration_0005_marks_placeholder_dates(tmp_path):
    path = (tmp_path / "m.db").as_posix()
    cfg = alembic_config(f"sqlite+aiosqlite:///{path}")
    command.upgrade(cfg, "0004")
    eng = sa.create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO tenant (id, key, name) VALUES (1, 't', 'T')"))
        c.execute(
            sa.text(
                "INSERT INTO league (id, key, name, platform, base_url, timezone, min_players, max_players, "
                "poll_weekday, poll_time, store_birth_date) VALUES (1, 'l', 'L', 'mtgame', 'u', 'Europe/Moscow', 5, 12, 2, '10:00', 0)"
            )
        )
        c.execute(sa.text("INSERT INTO tournament (id, league_id, external_id) VALUES (1, 1, 't')"))
        c.execute(
            sa.text("INSERT INTO league_team (id, league_id, external_id, name) VALUES (1, 1, 'a', 'A')")
        )
        for mid, day in [(7, "0001-01-01"), (8, "2026-10-24")]:
            c.execute(
                sa.text(
                    "INSERT INTO match (id, tournament_id, external_id, home_team_id, away_team_id, match_date, "
                    "time_status, status, updated_at) VALUES (:id, 1, :ext, 1, 1, :d, 'date_only', 'scheduled', '2026-10-19')"
                ),
                {"id": mid, "ext": f"g{mid}", "d": day},
            )
    eng.dispose()

    command.upgrade(cfg, "head")
    eng = sa.create_engine(f"sqlite:///{path}")
    with eng.connect() as c:
        flags = dict(c.execute(sa.text("SELECT id, date_is_estimate FROM match")).all())
    eng.dispose()
    assert flags == {7: 1, 8: 0}
