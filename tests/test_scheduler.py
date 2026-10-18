"""Планировщик: слоты синхронизации, однократность, ошибки источника, сообщения об изменениях матчей,
обновление действующих анонсов, изоляция тенантов."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from basket_assist.channels.base import ChannelError
from basket_assist.domain.models import ChangeKind
from basket_assist.services import scheduler
from basket_assist.services.sync import SyncResult
from basket_assist.sources.base import SourceError
from basket_assist.storage.bootstrap import apply_tenant_config
from basket_assist.storage.tables import (
    ChangeEvent,
    Chat,
    League,
    LeagueTeam,
    Match,
    PollMessage,
    TeamEntry,
    Tenant,
    Tournament,
    utcnow,
)
from tests.fakes import FakeChannel

CHAT = "2000000001"  # из config/tenant.example.toml
OTHER_CHAT = "2000000002"
MSK = ZoneInfo("Europe/Moscow")


def msk(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=MSK)


# ---------- due_jobs ----------


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (msk(2026, 10, 21, 3, 59), []),  # ср, до ночной — ничего
        (msk(2026, 10, 21, 4, 0), ["full"]),
        (msk(2026, 10, 21, 8, 59), ["full"]),
        (msk(2026, 10, 21, 9, 0), ["full", "matches"]),
        (msk(2026, 10, 21, 23, 59), ["full", "matches"]),
        (msk(2026, 10, 24, 12, 0), ["full", "matches"]),  # сб
        (msk(2026, 10, 19, 12, 0), ["full"]),  # пн
        (msk(2026, 10, 20, 12, 0), ["full"]),  # вт
        (msk(2026, 10, 25, 12, 0), ["full"]),  # вс
        (msk(2026, 10, 25, 3, 0), []),
    ],
)
def test_due_jobs(local, expected):
    assert [k for k, _ in scheduler.due_jobs(local)] == expected


def test_due_jobs_slot_keys():
    jobs = dict(scheduler.due_jobs(msk(2026, 10, 21, 9, 30)))
    assert jobs == {"full": "full:2026-10-21", "matches": "matches:2026-10-21T09"}
    # другая минута того же часа — тот же слот
    assert dict(scheduler.due_jobs(msk(2026, 10, 21, 9, 59))) == jobs


# ---------- tick ----------


class FakeSync:
    def __init__(self, fail: dict[int, Exception] | None = None):
        self.calls: list[tuple[int, bool, bool]] = []  # (entry_id, roster, standings)
        self.fail = fail or {}

    async def __call__(self, session, entry, adapter, matches=True, roster=True, standings=True):
        self.calls.append((entry.id, roster, standings))
        if entry.id in self.fail:
            raise self.fail[entry.id]
        return SyncResult()


@pytest.fixture
def fake_sync(monkeypatch):
    def install(**kw):
        fs = FakeSync(**kw)
        monkeypatch.setattr(scheduler, "sync_team_entry", fs)
        monkeypatch.setattr(scheduler, "make_adapter", lambda *a, **k: object())
        return fs

    return install


async def _entries(session, tenant_cfg):
    await apply_tenant_config(session, tenant_cfg)
    await session.commit()
    return [e.id for e in (await session.scalars(select(TeamEntry).order_by(TeamEntry.id))).all()]


async def test_tick_runs_each_slot_once(session, tenant_cfg, fake_sync):
    ids = await _entries(session, tenant_cfg)
    fs = fake_sync()
    ch = FakeChannel()
    async with httpx.AsyncClient() as client:
        wed10 = msk(2026, 10, 21, 10).astimezone(UTC)
        await scheduler.tick(session, ch, client, wed10)
        # полная синхронизация по обоим выступлениям, почасовая следом не идёт
        assert fs.calls == [(i, True, True) for i in ids]

        await scheduler.tick(session, ch, client, wed10 + timedelta(minutes=1))
        await scheduler.tick(session, ch, client, wed10 + timedelta(minutes=59))
        assert len(fs.calls) == len(ids)  # тот же слот — повторно не синхронизирует

        await scheduler.tick(session, ch, client, wed10 + timedelta(hours=1))
        assert fs.calls[len(ids) :] == [(i, False, False) for i in ids]  # только календарь

        await scheduler.tick(session, ch, client, wed10 + timedelta(days=1))  # чт 10:00 — новая полная
        assert fs.calls[2 * len(ids) :] == [(i, True, True) for i in ids]


async def test_tick_night_and_off_days(session, tenant_cfg, fake_sync):
    ids = await _entries(session, tenant_cfg)
    fs = fake_sync()
    ch = FakeChannel()
    async with httpx.AsyncClient() as client:
        await scheduler.tick(session, ch, client, msk(2026, 10, 19, 3, 30).astimezone(UTC))  # пн ночь
        assert fs.calls == []
        await scheduler.tick(session, ch, client, msk(2026, 10, 19, 4, 0).astimezone(UTC))
        assert len(fs.calls) == len(ids)
        await scheduler.tick(session, ch, client, msk(2026, 10, 19, 12, 0).astimezone(UTC))  # пн днём
        assert len(fs.calls) == len(ids)  # почасовой в понедельник нет


@pytest.mark.parametrize(
    "error", [SourceError("mtgame", "сайт сломан"), httpx.ConnectError("нет сети")], ids=["source", "http"]
)
async def test_tick_source_error_does_not_stop_other_entries(session, tenant_cfg, fake_sync, error):
    ids = await _entries(session, tenant_cfg)
    fs = fake_sync(fail={ids[0]: error})
    ch = FakeChannel()
    async with httpx.AsyncClient() as client:
        wed10 = msk(2026, 10, 21, 10).astimezone(UTC)
        await scheduler.tick(session, ch, client, wed10)
        assert [c[0] for c in fs.calls] == ids  # второе выступление синхронизировано
        # слот упавшего занят: сайт не долбим каждую минуту
        await scheduler.tick(session, ch, client, wed10 + timedelta(minutes=1))
        assert len(fs.calls) == len(ids)


# ---------- process_changes ----------


async def _abl(session, tenant_cfg):
    await apply_tenant_config(session, tenant_cfg)
    league = (await session.execute(select(League).filter_by(key="abl"))).scalar_one()
    tournament = (await session.execute(select(Tournament).filter_by(league_id=league.id))).scalar_one()
    ours = (
        await session.execute(select(LeagueTeam).filter_by(league_id=league.id, external_id="279"))
    ).scalar_one()
    rival = LeagueTeam(league_id=league.id, external_id="2301", name="Pokrasote")
    session.add(rival)
    await session.flush()
    return league, tournament, ours, rival


def _match(tournament, home, away, ext="1", start=datetime(2026, 10, 24, 12, 0), status="scheduled"):
    return Match(
        tournament_id=tournament.id,
        external_id=ext,
        home_team_id=home.id,
        away_team_id=away.id,
        match_date=start.date(),
        start_at=start,
        time_status="time_set",
        status=status,
        venue_name="Зал",
    )


async def test_process_changes_texts_and_order(session, tenant_cfg):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    m_home = _match(t, ours, rival, "1")
    m_away = _match(t, rival, ours, "2")  # мы в гостях — хозяин слева
    session.add_all([m_home, m_away])
    await session.flush()
    events = [
        ChangeEvent(
            kind=ChangeKind.RESCHEDULED, match_id=m_away.id, payload=json.dumps({"was": "2026-10-18"})
        ),
        ChangeEvent(kind=ChangeKind.TIME_CHANGED, match_id=m_home.id),
        ChangeEvent(kind=ChangeKind.TIME_SET, match_id=m_home.id),
        ChangeEvent(kind=ChangeKind.MATCH_ADDED, match_id=m_away.id),
        ChangeEvent(kind=ChangeKind.ROSTER_ADDED, league_team_id=ours.id),
        ChangeEvent(kind=ChangeKind.ROSTER_REMOVED, league_team_id=ours.id),
    ]
    session.add_all(events)
    await session.commit()

    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    texts = [t for _, t, _ in ch.sent]
    assert {p for p, _, _ in ch.sent} == {CHAT}
    when = "сб 24.10, 15:00"
    assert texts == [
        f"⚠️ Перенос матча: Pokrasote — Westchester (АБЛ) — теперь {when}. Было: 18.10."
        " Проверьте свою отметку в анонсе.",
        f"⏰ Изменилось время матча: Westchester — Pokrasote (АБЛ) — теперь {when}."
        " Проверьте свою отметку в анонсе.",
        f"⏰ Назначено время матча: Westchester — Pokrasote (АБЛ) — {when}.",
        f"📅 В календаре лиги новый матч: Pokrasote — Westchester (АБЛ) — {when}.",
    ]
    for ev in events:
        await session.refresh(ev)
    assert [e.processed for e in events] == [True, True, True, True, False, False]

    await scheduler.process_changes(session, ch)  # повторно — ничего нового
    assert len(ch.sent) == 4


async def test_process_changes_skips_finished_and_no_chat(session, tenant_cfg):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    played = _match(t, ours, rival, "1", status="played")
    cancelled = _match(t, ours, rival, "2", status="cancelled")
    live = _match(t, ours, rival, "3")
    session.add_all([played, cancelled, live])
    await session.flush()
    evs = [ChangeEvent(kind=ChangeKind.TIME_CHANGED, match_id=m.id) for m in (played, cancelled)]
    session.add_all(evs)
    await session.commit()
    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    assert ch.sent == []
    for ev in evs:
        await session.refresh(ev)
        assert ev.processed

    # беседы нет — без падения, событие помечено обработанным
    await session.delete((await session.execute(select(Chat))).scalar_one())
    ev = ChangeEvent(kind=ChangeKind.TIME_SET, match_id=live.id)
    session.add(ev)
    await session.commit()
    await scheduler.process_changes(session, ch)
    assert ch.sent == []
    await session.refresh(ev)
    assert ev.processed


async def test_process_changes_channel_error_does_not_raise(session, tenant_cfg):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    m = _match(t, ours, rival)
    session.add(m)
    await session.flush()
    session.add(ChangeEvent(kind=ChangeKind.TIME_SET, match_id=m.id))
    await session.commit()

    class Broken(FakeChannel):
        async def send(self, peer_id, text, buttons=None):
            raise ChannelError(917, "нет доступа к беседе")

    await scheduler.process_changes(session, Broken())
    ev = (await session.scalars(select(ChangeEvent))).one()
    assert ev.processed


async def test_process_changes_tenant_isolation(session, tenant_cfg):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    other = Tenant(key="other", name="Другая команда")
    session.add(other)
    await session.flush()
    other_team = LeagueTeam(league_id=t.league_id, external_id="5000", name="Ракета")
    session.add(other_team)
    await session.flush()
    session.add_all(
        [
            TeamEntry(tenant_id=other.id, league_team_id=other_team.id, tournament_id=t.id, source_url="x"),
            Chat(tenant_id=other.id, channel="vk", peer_id=OTHER_CHAT),
        ]
    )
    m_other = _match(t, other_team, rival, "10")  # матч только другой команды
    m_derby = _match(t, ours, other_team, "11")  # обе команды сервиса
    session.add_all([m_other, m_derby])
    await session.flush()
    session.add_all(
        [
            ChangeEvent(kind=ChangeKind.TIME_SET, match_id=m_other.id),
            ChangeEvent(kind=ChangeKind.TIME_CHANGED, match_id=m_derby.id),
        ]
    )
    await session.commit()

    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    peers = [p for p, _, _ in ch.sent]
    assert peers.count(OTHER_CHAT) == 2 and peers.count(CHAT) == 1
    ours_texts = [t for p, t, _ in ch.sent if p == CHAT]
    assert ours_texts == [ours_texts[0]] and "Westchester — Ракета" in ours_texts[0]


# ---------- refresh_active_polls ----------


async def _poll(session, match, chat, ref, posted_ago=timedelta(hours=1), edited=None):
    p = PollMessage(
        tenant_id=chat.tenant_id,
        match_id=match.id,
        chat_id=chat.id,
        message_ref=ref,
        posted_at=utcnow() - posted_ago,
        last_edited_at=edited,
    )
    session.add(p)
    await session.flush()
    return p


async def test_refresh_active_polls(session, tenant_cfg):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    chat = (await session.execute(select(Chat))).scalar_one()
    now = utcnow()
    changed = _match(t, ours, rival, "1", start=now + timedelta(days=2))
    unchanged = _match(t, ours, rival, "2", start=now + timedelta(days=3))
    no_time = _match(t, ours, rival, "3", start=now + timedelta(days=4))
    no_time.start_at = None
    started = _match(t, ours, rival, "4", start=now - timedelta(hours=1))  # идёт — ещё обновляем
    old = _match(t, ours, rival, "5", start=now - timedelta(hours=3))  # давно прошёл
    played = _match(t, ours, rival, "6", start=now + timedelta(days=1), status="played")
    session.add_all([changed, unchanged, no_time, started, old, played])
    await session.flush()
    for m, ago in ((changed, 10), (unchanged, 120), (no_time, 10), (started, 10), (old, 10), (played, 10)):
        m.updated_at = now - timedelta(minutes=ago)
    await _poll(session, changed, chat, "201", posted_ago=timedelta(hours=1))
    await _poll(
        session, unchanged, chat, "202", posted_ago=timedelta(hours=3), edited=now - timedelta(hours=1)
    )
    await _poll(session, no_time, chat, "203", posted_ago=timedelta(minutes=5))  # опубликован после изменения
    await _poll(session, started, chat, "204")
    await _poll(session, old, chat, "205")
    await _poll(session, played, chat, "206")
    await session.commit()

    ch = FakeChannel()
    assert await scheduler.refresh_active_polls(session, ch, only_changed=True) == 2
    assert sorted(r.message_id for r, _ in ch.edits) == ["201", "204"]

    ch = FakeChannel()
    assert await scheduler.refresh_active_polls(session, ch) == 4  # все предстоящие и идущий
    assert sorted(r.message_id for r, _ in ch.edits) == ["201", "202", "203", "204"]

    # после правки анонса only_changed его больше не трогает
    ch = FakeChannel()
    assert await scheduler.refresh_active_polls(session, ch, only_changed=True) == 0


async def test_refresh_active_polls_superseded_and_channel_error(session, tenant_cfg):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    chat = (await session.execute(select(Chat))).scalar_one()
    m = _match(t, ours, rival, start=utcnow() + timedelta(days=2))
    session.add(m)
    await session.flush()
    old = await _poll(session, m, chat, "301")
    new = await _poll(session, m, chat, "302")
    old.superseded_by_id = new.id
    await session.commit()

    ch = FakeChannel()
    assert await scheduler.refresh_active_polls(session, ch) == 1
    assert [r.message_id for r, _ in ch.edits] == ["302"]

    class Broken(FakeChannel):
        async def edit(self, msg, text, buttons=None):
            raise ChannelError(15, "нет доступа")

    assert await scheduler.refresh_active_polls(session, Broken()) == 0


async def test_refresh_active_polls_tenant_isolation(session, tenant_cfg):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    other = Tenant(key="other", name="Другая команда")
    session.add(other)
    await session.flush()
    other_team = LeagueTeam(league_id=t.league_id, external_id="5000", name="Ракета")
    session.add(other_team)
    await session.flush()
    other_chat = Chat(tenant_id=other.id, channel="vk", peer_id=OTHER_CHAT)
    session.add_all(
        [
            other_chat,
            TeamEntry(tenant_id=other.id, league_team_id=other_team.id, tournament_id=t.id, source_url="x"),
        ]
    )
    m = _match(t, other_team, rival, "10", start=utcnow() + timedelta(days=2))
    session.add(m)
    await session.flush()
    await _poll(session, m, other_chat, "401")
    await session.commit()

    ch = FakeChannel()
    assert await scheduler.refresh_active_polls(session, ch) == 1
    assert [(r.peer_id, r.message_id) for r, _ in ch.edits] == [(OTHER_CHAT, "401")]
    assert "Ракета — Pokrasote" in ch.edits[0][1]
