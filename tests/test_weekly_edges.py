"""M4: граничные случаи недельного цикла — матч в пн/вт, время появилось в четверг / меньше чем за сутки,
перенос даты после сводки, перезапуск процесса, часовой пояс лиги (domain.md, «Жизненный цикл матча»,
«Правила»). Вымышленные имена."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from basket_assist.services import weekly
from basket_assist.storage import db
from basket_assist.storage.tables import Match, NotificationLog, PollMessage
from tests.fakes import FakeChannel
from tests.test_leader_bot import PETROV, _ready
from tests.test_roster import CAPTAIN, CHAT, VICE, _player
from tests.test_weekly import (
    D2,
    D24,
    NO_TIME,
    REMIND,
    SAT,
    THU,
    WED,
    _league,
    _texts,
    _to,
    _vote,
    _week_match,
    at,
    naive_utc,
)

FRI = date(2026, 10, 23)
MON, TUE = date(2026, 10, 26), date(2026, 10, 27)


def _digests(ch: FakeChannel, peer: str) -> list[str]:
    return [t.split("\n", 1)[0] for t in _texts(ch, peer, "📋")]


# ---------- матч в понедельник / вторник ----------


@pytest.mark.parametrize("day", [MON, TUE])
async def test_monday_tuesday_match_poll_on_previous_wednesday(session, tenant_cfg, day):
    _, entries = await _ready(session, tenant_cfg)
    await _week_match(session, entries["abl"], "g1", day=day, hhmm=(20, 0))
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 9, 59))
    assert _to(ch, CHAT) == []
    await weekly.tick(session, ch, now=at(WED, 10))
    assert len(_to(ch, CHAT)) == 1, "среда предыдущей недели"
    await weekly.tick(session, ch, now=at(date(2026, 10, 28), 10))  # следующая среда — уже после игры
    assert len(_to(ch, CHAT)) == 1


async def test_monday_match_without_time_warns_from_previous_wednesday(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    await _week_match(session, entries["abl"], "g1", day=MON, hhmm=None)
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    for d in (THU, SAT, MON):
        await weekly.tick(session, ch, now=at(d, 9))  # повтор — не раньше 10:00 по времени лиги
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 1
    for d in (THU, SAT, MON):
        await weekly.tick(session, ch, now=at(d, 10))
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 4
    await weekly.tick(session, ch, now=at(TUE, 10))  # день игры прошёл
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 4


# ---------- время появилось позже среды ----------


async def test_time_appears_on_thursday_poll_then_regular_digests(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1", hhmm=None)
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 1 and _to(ch, CHAT) == []

    m.start_at, m.time_status = naive_utc(at(SAT, 12)), "time_set"
    await session.commit()
    await weekly.tick(session, ch, now=at(THU, 14, 3))
    assert len(_to(ch, CHAT)) == 1, "опрос сразу на ближайшей минуте"
    assert _digests(ch, CAPTAIN) == [], "до игры больше суток — сводка по расписанию"
    await weekly.tick(session, ch, now=at(FRI, 9))
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 1, "время есть — предупреждений больше нет"
    await weekly.tick(session, ch, now=at(FRI, 12))
    assert _digests(ch, CAPTAIN) == [D24]


async def test_time_set_less_than_24h_digest_right_after_poll(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1", hhmm=None)
    m.start_at, m.time_status = naive_utc(at(SAT, 12)), "time_set"
    await session.commit()
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(FRI, 20))  # пт 20:00, до игры 16 ч
    assert len(_to(ch, CHAT)) == 1
    assert _digests(ch, CAPTAIN) == [D24] and _digests(ch, VICE) == [D24]
    first_chat = next(i for i, (p, _, _) in enumerate(ch.sent) if p == CHAT)
    first_digest = next(i for i, (p, t, _) in enumerate(ch.sent) if p == CAPTAIN and t.startswith(D24))
    assert first_chat < first_digest, "сводка — после запуска опроса"
    await weekly.tick(session, ch, now=at(SAT, 10))
    assert _digests(ch, CAPTAIN) == [D24, D2]


async def test_time_set_less_than_2h_only_final_digest(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1", hhmm=None)
    m.start_at, m.time_status = naive_utc(at(SAT, 12)), "time_set"
    await session.commit()
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(SAT, 10, 30))
    assert len(_to(ch, CHAT)) == 1 and _digests(ch, CAPTAIN) == [D2]


# ---------- перенос даты после сводки ----------


async def test_reschedule_after_digest24_new_digests_for_new_date(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    await weekly.tick(session, ch, now=at(FRI, 12))  # сводка за сутки
    assert _digests(ch, CAPTAIN) == [D24]

    new_day = date(2026, 10, 31)  # лига перенесла на следующую субботу (как sync.py)
    m.match_date, m.start_at, m.status = new_day, naive_utc(at(new_day, 12)), "rescheduled"
    await session.commit()
    await weekly.tick(session, ch, now=at(SAT, 10))  # старая дата: финальной быть не должно
    assert _digests(ch, CAPTAIN) == [D24]
    assert len((await session.scalars(select(PollMessage))).all()) == 1, "анонс тот же, отметки сохранены"

    await weekly.tick(session, ch, now=at(date(2026, 10, 30), 12))
    assert _digests(ch, CAPTAIN) == [D24, D24], "сводка за сутки до новой даты"
    await weekly.tick(session, ch, now=at(new_day, 10))
    assert _digests(ch, CAPTAIN) == [D24, D24, D2]


async def test_time_change_same_day_after_digest24_final_by_new_time(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(FRI, 12))
    assert _digests(ch, CAPTAIN) == [D24]
    m.start_at, m.time_status = naive_utc(at(SAT, 18)), "time_changed"
    await session.commit()
    await weekly.tick(session, ch, now=at(SAT, 10))  # старое T−2 ч
    assert _digests(ch, CAPTAIN) == [D24]
    await weekly.tick(session, ch, now=at(SAT, 16))  # новое T−2 ч
    assert _digests(ch, CAPTAIN) == [D24, D2]


async def test_time_change_same_day_after_digest24_no_second_daily(session, tenant_cfg):
    """Время сдвинули в тот же день (12:00 → 18:00) после суточной: тот же цикл — суточная и напоминание
    по новому T−24 ч / T−30 ч не повторяются, финальная — по новому времени."""
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    await weekly.tick(session, ch, now=at(FRI, 6))  # T−30 ч: напоминание
    await weekly.tick(session, ch, now=at(FRI, 12))  # T−24 ч: суточная
    assert _digests(ch, CAPTAIN) == [D24]
    reminders = sum(t.startswith(REMIND) for _, t, _ in ch.sent)
    assert reminders > 0
    m.start_at, m.time_status = naive_utc(at(SAT, 18)), "time_changed"
    await session.commit()
    for now in (at(FRI, 12, 1), at(FRI, 18), at(FRI, 18, 1)):  # новое T−24 ч
        await weekly.tick(session, ch, now=now)
    assert _digests(ch, CAPTAIN) == [D24], "суточная по тому же дню не повторяется"
    assert sum(t.startswith(REMIND) for _, t, _ in ch.sent) == reminders, "напоминание тоже"
    keys = (await session.scalars(select(NotificationLog.dedup_key).filter_by(kind="digest24"))).all()
    assert keys == [weekly.cycle_key(m)] == [f"{m.id}:{SAT.isoformat()}"]
    await weekly.tick(session, ch, now=at(SAT, 16))
    assert _digests(ch, CAPTAIN) == [D24, D2]


async def test_reschedule_new_cycle_reminds_again(session, tenant_cfg):
    """Перенос на другую дату после напоминания — новый цикл: напоминание молчащим по новой дате уходит снова."""
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    await weekly.tick(session, ch, now=at(FRI, 6))
    first = sum(t.startswith(REMIND) for _, t, _ in ch.sent)
    assert first > 0
    new_day = date(2026, 10, 31)
    m.match_date, m.start_at, m.status = new_day, naive_utc(at(new_day, 12)), "rescheduled"
    await session.commit()
    await weekly.tick(session, ch, now=at(FRI, 7))
    assert sum(t.startswith(REMIND) for _, t, _ in ch.sent) == first, "до нового T−30 ч — тишина"
    await weekly.tick(session, ch, now=at(date(2026, 10, 30), 6))
    assert sum(t.startswith(REMIND) for _, t, _ in ch.sent) == 2 * first
    keys = sorted((await session.scalars(select(NotificationLog.dedup_key).filter_by(kind="remind30"))).all())
    assert keys == [f"{m.id}:2026-10-24", f"{m.id}:2026-10-31"]


# ---------- перезапуск процесса ----------


async def test_restart_does_not_duplicate(db_url, tenant_cfg):
    """Каждый прогон — новый процесс: свой движок, сессия и канал; общее — только БД."""
    moments = [at(WED, 10), at(FRI, 6), at(FRI, 12), at(SAT, 10)]
    sent: list[list] = []
    engine = db.make_engine(db_url)
    async with db.make_sessionmaker(engine)() as s:
        _, entries = await _ready(s, tenant_cfg)
        await _week_match(s, entries["abl"], "g1")
        await _week_match(s, entries["abl"], "g2", hhmm=None)
    await engine.dispose()
    for _run in range(2):
        for now in moments:
            engine = db.make_engine(db_url)
            async with db.make_sessionmaker(engine)() as s:
                ch = FakeChannel()
                await weekly.tick(s, ch, now=now)
                await weekly.tick(s, ch, now=now + timedelta(minutes=1))
                sent.append(ch.sent)
            await engine.dispose()
    first, second = sent[: len(moments)], sent[len(moments) :]
    assert all(batch == [] for batch in second), "повторный проход по тем же моментам ничего не шлёт"
    flat = [t for batch in first for p, t, _ in batch if p == CAPTAIN]
    assert sum(t.startswith(D24) for t in flat) == 1 and sum(t.startswith(D2) for t in flat) == 1
    assert sum(t.startswith(REMIND) for t in flat) == 1
    assert sum(t.startswith(NO_TIME) for t in flat) == 3, "ср, пт, сб — по разу в сутки"
    assert sum(p == CHAT for batch in first for p, _, _ in batch) == 1, "один анонс (g1)"


# ---------- часовой пояс лиги ----------


async def test_no_time_day_boundary_in_league_timezone(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    league = await _league(session, entries["abl"])
    league.timezone = "Asia/Vladivostok"  # UTC+10
    await session.commit()
    await _week_match(session, entries["abl"], "g1", hhmm=None)
    ch = FakeChannel()
    await weekly.tick(session, ch, now=datetime(2026, 10, 21, 0, 0, tzinfo=UTC))  # ср 10:00 Владивосток
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 1
    await weekly.tick(session, ch, now=datetime(2026, 10, 21, 13, 59, tzinfo=UTC))  # ср 23:59
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 1
    await weekly.tick(session, ch, now=datetime(2026, 10, 21, 14, 0, tzinfo=UTC))  # чт 00:00 там, ср по UTC
    await weekly.tick(session, ch, now=datetime(2026, 10, 21, 23, 59, tzinfo=UTC))  # чт 09:59 там
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 1, "новые сутки лиги, но до 10:00 по её времени — не будим"
    await weekly.tick(session, ch, now=datetime(2026, 10, 22, 0, 0, tzinfo=UTC))  # чт 10:00 там
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 2
    keys = (await session.scalars(select(NotificationLog.dedup_key).filter_by(kind="no_time"))).all()
    assert sorted(k.split(":")[1] for k in keys) == ["2026-10-21", "2026-10-22"]
    await weekly.tick(session, ch, now=datetime(2026, 10, 24, 14, 0, tzinfo=UTC))  # вс 00:00 там
    await weekly.tick(session, ch, now=datetime(2026, 10, 25, 0, 0, tzinfo=UTC))  # вс 10:00 там
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 2, "вс по времени лиги — матч прошёл"


async def test_poll_opens_across_dst_in_league_timezone(session, tenant_cfg):
    """Лига с переходом на зимнее время (25.10.2026 в Европе): среда после перехода — по новому смещению."""
    _, entries = await _ready(session, tenant_cfg)
    league = await _league(session, entries["abl"])
    league.timezone = "Europe/Berlin"
    await session.commit()
    day = date(2026, 10, 31)
    rival_m = await _week_match(session, entries["abl"], "g1", day=day)
    assert weekly.poll_opens_at(rival_m, league) == datetime(2026, 10, 28, 9, 0)  # 10:00 CET = 09:00 UTC
    ch = FakeChannel()
    await weekly.tick(session, ch, now=datetime(2026, 10, 28, 8, 59, tzinfo=UTC))
    assert _to(ch, CHAT) == []
    await weekly.tick(session, ch, now=datetime(2026, 10, 28, 9, 0, tzinfo=UTC))
    assert len(_to(ch, CHAT)) == 1


async def test_digest_times_absolute_regardless_of_league_timezone(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    league = await _league(session, entries["abl"])
    league.timezone = "Asia/Yekaterinburg"
    await session.commit()
    m = await _week_match(session, entries["abl"], "g1")
    start = m.start_at.replace(tzinfo=UTC)
    ch = FakeChannel()
    await weekly.tick(session, ch, now=start - timedelta(hours=24, minutes=1))
    assert _digests(ch, CAPTAIN) == []
    await weekly.tick(session, ch, now=start - timedelta(hours=24))
    assert _digests(ch, CAPTAIN) == [D24]
    assert (await session.get(Match, m.id)).start_at == m.start_at
