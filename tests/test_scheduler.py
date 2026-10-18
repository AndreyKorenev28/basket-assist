"""Планировщик: слоты синхронизации, однократность, ошибки источника, сообщения об изменениях матчей,
обновление действующих анонсов, изоляция тенантов."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from basket_assist.channels.base import ChannelError
from basket_assist.domain.models import ChangeKind, Vote
from basket_assist.services import scheduler
from basket_assist.services.sync import SyncResult
from basket_assist.sources.base import SourceError
from basket_assist.storage.bootstrap import apply_tenant_config
from basket_assist.storage.tables import (
    Attendance,
    ChangeEvent,
    Chat,
    League,
    LeagueTeam,
    Match,
    MessengerAccount,
    NotificationLog,
    Player,
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
        (msk(2026, 10, 23, 12, 0), ["full", "matches"]),  # пт — последний день почасовой
        (msk(2026, 10, 24, 12, 0), ["full"]),  # сб — в выходные почасовой нет
        (msk(2026, 10, 24, 23, 0), ["full"]),
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

        # суббота: ночная полная есть, почасовой нет
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 4, 0).astimezone(UTC))
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 12, 0).astimezone(UTC))
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 13, 0).astimezone(UTC))
        assert fs.calls[len(ids) :] == [(i, True, True) for i in ids]


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


# ---------- личка отметившимся при переносе / смене времени ----------

ASK = "Если планы изменились — нажмите другую кнопку в анонсе в беседе команды."


async def _voter(session, tenant_id, match, name, vk, vote=None, dm=True):
    p = Player(tenant_id=tenant_id, display_name=name)
    session.add(p)
    await session.flush()
    if vk:
        session.add(
            MessengerAccount(
                tenant_id=tenant_id, player_id=p.id, channel="vk", external_user_id=vk, dm_allowed=dm
            )
        )
    if vote:
        session.add(Attendance(tenant_id=tenant_id, match_id=match.id, player_id=p.id, vote=vote))
    await session.flush()
    return p


async def _votes(session):
    return sorted((a.player_id, a.vote) for a in (await session.scalars(select(Attendance))).all())


async def _our_tenant(session):
    return (await session.scalars(select(TeamEntry).order_by(TeamEntry.id))).first().tenant_id


@pytest.mark.parametrize(
    ("kind", "payload", "head"),
    [
        (
            ChangeKind.RESCHEDULED,
            json.dumps({"was": "2026-10-18"}),
            "⚠️ Перенос матча: Westchester — Pokrasote (АБЛ) — теперь сб 24.10, 15:00. Было: 18.10.",
        ),
        (
            ChangeKind.TIME_CHANGED,
            None,
            "⏰ Изменилось время матча: Westchester — Pokrasote (АБЛ) — теперь сб 24.10, 15:00.",
        ),
    ],
    ids=["rescheduled", "time_changed"],
)
async def test_process_changes_dm_voters(session, tenant_cfg, kind, payload, head):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    m = _match(t, ours, rival)
    other_m = _match(t, ours, rival, "2")
    session.add_all([m, other_m])
    await session.flush()
    tid = await _our_tenant(session)
    await _voter(session, tid, m, "Иван Петров", "101", Vote.GOING)
    await _voter(session, tid, m, "Пётр Сидоров", "102", Vote.NOT_GOING)
    await _voter(session, tid, m, "Олег Тестов", "103")  # не отметился
    await _voter(session, tid, m, "Илья Кузнецов", "104", Vote.GOING, dm=False)  # лички нет
    await _voter(session, tid, m, "Антон Попов", None, Vote.GOING)  # нет аккаунта VK
    await _voter(session, tid, other_m, "Глеб Волков", "105", Vote.GOING)  # отметка на другой матч
    session.add(ChangeEvent(kind=kind, match_id=m.id, payload=payload))
    await session.commit()
    before = await _votes(session)

    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    chat_texts = [txt for p, txt, _ in ch.sent if p == CHAT]
    assert chat_texts == [f"{head} Проверьте свою отметку в анонсе."]  # беседа — как раньше
    dms = sorted((p, txt) for p, txt, _ in ch.sent if p != CHAT)
    assert dms == [
        ("101", f"{head}\nВаш ответ сохранён: «Буду». {ASK}"),
        ("102", f"{head}\nВаш ответ сохранён: «Не буду». {ASK}"),
    ]
    assert await _votes(session) == before  # отметки не меняются

    # то же событие снова попало в обработку — лички не дублируются (notification_log)
    ev = (await session.scalars(select(ChangeEvent))).one()
    ev.processed = False
    await session.commit()
    ch2 = FakeChannel()
    await scheduler.process_changes(session, ch2)
    assert [p for p, _, _ in ch2.sent] == [CHAT]
    await scheduler.process_changes(session, ch2)  # обычный повтор — ничего
    assert len(ch2.sent) == 1


@pytest.mark.parametrize("kind", [ChangeKind.TIME_SET, ChangeKind.MATCH_ADDED])
async def test_process_changes_no_dm_for_time_set_and_added(session, tenant_cfg, kind):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    m = _match(t, ours, rival)
    session.add(m)
    await session.flush()
    await _voter(session, await _our_tenant(session), m, "Иван Петров", "101", Vote.GOING)
    session.add(ChangeEvent(kind=kind, match_id=m.id))
    await session.commit()
    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    assert [p for p, _, _ in ch.sent] == [CHAT]


async def test_process_changes_dm_without_chat(session, tenant_cfg):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    m = _match(t, ours, rival)
    session.add(m)
    await session.flush()
    await _voter(session, await _our_tenant(session), m, "Иван Петров", "101", Vote.GOING)
    await session.delete((await session.execute(select(Chat))).scalar_one())
    session.add(ChangeEvent(kind=ChangeKind.TIME_CHANGED, match_id=m.id))
    await session.commit()
    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    assert [p for p, _, _ in ch.sent] == ["101"]
    assert "«Буду»" in ch.sent[0][1]


async def test_process_changes_dm_tenant_isolation(session, tenant_cfg):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    our_tid = await _our_tenant(session)
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
    # отметка в нашем тенанте на матч другой команды — личку о нём не получает
    await _voter(session, our_tid, m_other, "Иван Петров", "101", Vote.GOING)
    await _voter(session, other.id, m_other, "Сергей Орлов", "201", Vote.NOT_GOING)
    await _voter(session, our_tid, m_derby, "Пётр Сидоров", "102", Vote.GOING)
    await _voter(session, other.id, m_derby, "Денис Лебедев", "202", Vote.GOING)
    session.add_all(
        [
            ChangeEvent(kind=ChangeKind.TIME_CHANGED, match_id=m_other.id),
            ChangeEvent(kind=ChangeKind.TIME_CHANGED, match_id=m_derby.id),
        ]
    )
    await session.commit()

    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    dms = {p: txt for p, txt, _ in ch.sent if p not in (CHAT, OTHER_CHAT)}
    assert set(dms) == {"201", "102", "202"}
    assert "Ракета — Pokrasote" in dms["201"] and "«Не буду»" in dms["201"]
    assert "Westchester — Ракета" in dms["102"] and "«Буду»" in dms["102"]
    assert "Westchester — Ракета" in dms["202"] and "«Буду»" in dms["202"]


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


# ---------- синхронизация результата через 3 ч после начала ----------

SAT_NIGHT = msk(2026, 10, 24, 4, 0).astimezone(UTC)  # ночная полная субботы — занимаем её заранее


def _utc_naive(local: datetime) -> datetime:
    return local.astimezone(UTC).replace(tzinfo=None)


async def _result_setup(session, tenant_cfg, starts):
    """Наши матчи в АБЛ с заданным началом (время лиги). Возвращает id выступлений."""
    _, t, ours, rival = await _abl(session, tenant_cfg)
    for i, start in enumerate(starts):
        session.add(_match(t, ours, rival, f"r{i}", start=_utc_naive(start)))
    await session.commit()
    return [e.id for e in (await session.scalars(select(TeamEntry).order_by(TeamEntry.id))).all()]


async def _job_keys(session):
    return sorted(
        (n.tenant_id, n.dedup_key)
        for n in (await session.scalars(select(NotificationLog).filter_by(kind="job"))).all()
    )


async def _result_keys(session):
    return [(tid, k) for tid, k in await _job_keys(session) if ":result:" in k]


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (timedelta(hours=2, minutes=59), False),
        (timedelta(hours=3), True),
        (timedelta(hours=11, minutes=59), True),
        (timedelta(hours=12), False),
    ],
    ids=["2:59", "3:00", "11:59", "12:00"],
)
async def test_result_window_bounds(session, tenant_cfg, fake_sync, after, expected):
    start = msk(2026, 10, 24, 12, 0)  # сб 12:00; +12 ч = вс 00:00 — до ночной, полная не мешает
    ids = await _result_setup(session, tenant_cfg, [start])
    fs = fake_sync()
    ch = FakeChannel()
    async with httpx.AsyncClient() as client:
        await scheduler.tick(session, ch, client, SAT_NIGHT)
        fs.calls.clear()
        await scheduler.tick(session, ch, client, start.astimezone(UTC) + after)
    assert fs.calls == ([(ids[0], False, True)] if expected else [])


async def test_result_sync_once_after_match(session, tenant_cfg, fake_sync):
    ids = await _result_setup(session, tenant_cfg, [msk(2026, 10, 24, 17, 0)])
    fs = fake_sync()
    ch = FakeChannel()
    async with httpx.AsyncClient() as client:
        await scheduler.tick(session, ch, client, SAT_NIGHT)
        assert fs.calls == [(i, True, True) for i in ids]
        fs.calls.clear()
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 20, 30).astimezone(UTC))
        assert fs.calls == [(ids[0], False, True)]  # календарь и таблица, без заявки на сезон
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 20, 31).astimezone(UTC))
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 21, 30).astimezone(UTC))
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 23, 59).astimezone(UTC))
        assert len(fs.calls) == 1  # одна попытка: счёта нет — ждём ночную
        # ночью матч ещё в окне, но слот результата уже занят — только полная
        await scheduler.tick(session, ch, client, msk(2026, 10, 25, 4, 0).astimezone(UTC))
        assert fs.calls[1:] == [(i, True, True) for i in ids]


async def test_result_sync_two_leagues(session, tenant_cfg, fake_sync):
    # два матча в АБЛ и один в МЛБЛ — по одной синхронизации на лигу
    ids = await _result_setup(session, tenant_cfg, [msk(2026, 10, 24, 17, 0), msk(2026, 10, 24, 16, 0)])
    mlbl = (await session.execute(select(League).filter_by(key="mlbl"))).scalar_one()
    mt = (await session.execute(select(Tournament).filter_by(league_id=mlbl.id))).scalar_one()
    m_ours = (
        await session.execute(select(LeagueTeam).filter_by(league_id=mlbl.id, external_id="9056"))
    ).scalar_one()
    m_rival = LeagueTeam(league_id=mlbl.id, external_id="7777", name="Комета")
    session.add(m_rival)
    await session.flush()
    session.add(_match(mt, m_rival, m_ours, "m1", start=_utc_naive(msk(2026, 10, 24, 15, 0))))  # в гостях
    await session.commit()
    fs = fake_sync()
    ch = FakeChannel()
    async with httpx.AsyncClient() as client:
        await scheduler.tick(session, ch, client, SAT_NIGHT)
        fs.calls.clear()
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 20, 30).astimezone(UTC))
        assert sorted(fs.calls) == sorted((i, False, True) for i in ids)
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 20, 31).astimezone(UTC))
        assert len(fs.calls) == 2
    assert len(await _result_keys(session)) == 3  # слоты всех трёх матчей заняты


@pytest.mark.parametrize("case", ["played", "forfeit", "cancelled", "no_time", "other_team"])
async def test_result_sync_skips(session, tenant_cfg, fake_sync, case):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    start = _utc_naive(msk(2026, 10, 24, 17, 0))
    if case == "other_team":  # матч двух чужих команд в нашем турнире
        rival2 = LeagueTeam(league_id=t.league_id, external_id="2302", name="Гроза")
        session.add(rival2)
        await session.flush()
        m = _match(t, rival, rival2, "x", start=start)
    elif case == "no_time":
        m = _match(t, ours, rival, "x", start=start)
        m.start_at = None
        m.time_status = "no_time"
    else:
        m = _match(t, ours, rival, "x", start=start, status=case)
    session.add(m)
    await session.commit()
    fs = fake_sync()
    ch = FakeChannel()
    async with httpx.AsyncClient() as client:
        await scheduler.tick(session, ch, client, SAT_NIGHT)
        fs.calls.clear()
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 20, 30).astimezone(UTC))
    assert fs.calls == []
    assert await _result_keys(session) == []


async def test_result_slot_taken_by_full(session, tenant_cfg, fake_sync):
    # бот запущен в вс 05:00 после матча сб 21:00 (8 ч назад, в окне): одна полная, слот результата занят
    ids = await _result_setup(session, tenant_cfg, [msk(2026, 10, 24, 21, 0)])
    fs = fake_sync()
    ch = FakeChannel()
    async with httpx.AsyncClient() as client:
        sun5 = msk(2026, 10, 25, 5, 0).astimezone(UTC)
        await scheduler.tick(session, ch, client, sun5)
        assert fs.calls == [(i, True, True) for i in ids]
        await scheduler.tick(session, ch, client, sun5 + timedelta(minutes=1))
        assert len(fs.calls) == len(ids)
    m_id = (await session.scalars(select(Match.id))).one()
    tid = await _our_tenant(session)
    assert await _result_keys(session) == [(tid, f"{ids[0]}:result:{m_id}")]


async def test_result_sync_tenant_isolation(session, tenant_cfg, fake_sync):
    _, t, ours, rival = await _abl(session, tenant_cfg)
    other = Tenant(key="other", name="Другая команда")
    session.add(other)
    await session.flush()
    other_team = LeagueTeam(league_id=t.league_id, external_id="5000", name="Ракета")
    session.add(other_team)
    await session.flush()
    other_entry = TeamEntry(
        tenant_id=other.id, league_team_id=other_team.id, tournament_id=t.id, source_url="x"
    )
    session.add(other_entry)
    m = _match(t, other_team, rival, "10", start=_utc_naive(msk(2026, 10, 24, 17, 0)))  # только другой
    session.add(m)
    await session.commit()
    fs = fake_sync()
    ch = FakeChannel()
    async with httpx.AsyncClient() as client:
        await scheduler.tick(session, ch, client, SAT_NIGHT)
        fs.calls.clear()
        await scheduler.tick(session, ch, client, msk(2026, 10, 24, 20, 30).astimezone(UTC))
    assert fs.calls == [(other_entry.id, False, True)]
    assert await _result_keys(session) == [(other.id, f"{other_entry.id}:result:{m.id}")]
