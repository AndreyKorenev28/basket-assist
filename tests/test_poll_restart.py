"""Решения владельца 2026-10-20: пауза на «игры» в беседе и перезапуск опроса при переносе / смене времени
(отметки сбрасываются, старый анонс закрывается, новый публикует недельный цикл). Вымышленные имена."""

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from basket_assist.bot import handlers, leader
from basket_assist.bot.handlers import GAMES_ABOVE, GAMES_REPEAT, handle
from basket_assist.channels.base import ButtonPress, IncomingMessage, MessageRef
from basket_assist.domain.models import ChangeKind, Vote
from basket_assist.services import polls, scheduler, weekly
from basket_assist.storage.tables import Attendance, ChangeEvent, Chat, PollMessage, utcnow
from tests.fakes import FakeChannel
from tests.test_digests import _match as _rel_match
from tests.test_leader_bot import PETROV, SIDOROV, _cmd_press, _ready
from tests.test_roster import CAPTAIN, CHAT, _player
from tests.test_weekly import REMIND, SAT, WED, _vote, _week_match, at, naive_utc

FRI = date(2026, 10, 23)
RESET = "Прежние отметки сброшены — отметьтесь заново в закреплённом анонсе."
AGAIN = "Будете в новое время?"


# ---------- пауза на «игры» в беседе ----------


@pytest.fixture
def clock(monkeypatch):
    """Часы паузы: handlers.utcnow; сдвигаются через clock[0]."""
    now = [utcnow()]
    monkeypatch.setattr(handlers, "utcnow", lambda: now[0])
    return now


def _games_text(user: str = CAPTAIN, peer: str = CHAT) -> IncomingMessage:
    return IncomingMessage(peer, user, "игры", is_chat=True, mentioned=True)


async def _run(session, event) -> FakeChannel:
    ch = FakeChannel()
    await handle(session, ch, event)
    return ch


async def test_games_repeat_button_toast_no_message(session, tenant_cfg, clock):
    await _ready(session, tenant_cfg)
    first = await _run(session, _games_text())
    assert [p for p, _, _ in first.sent] == [CHAT]
    clock[0] += GAMES_REPEAT - timedelta(seconds=1)
    ch = await _run(session, _cmd_press(CHAT, PETROV, "игры"))  # другой игрок — пауза общая на беседу
    assert ch.sent == [] and ch.toasts == [GAMES_ABOVE]


async def test_games_repeat_text_is_silent(session, tenant_cfg, clock):
    await _ready(session, tenant_cfg)
    await _run(session, _cmd_press(CHAT, CAPTAIN, "игры"))
    for word in ("игры", "матчи", "расписание"):
        clock[0] += timedelta(minutes=1)
        ch = await _run(session, IncomingMessage(CHAT, SIDOROV, word, is_chat=True, mentioned=True))
        assert ch.sent == [] and ch.toasts == [], word


async def test_games_after_pause_sent_again(session, tenant_cfg, clock):
    await _ready(session, tenant_cfg)
    first = await _run(session, _games_text())
    clock[0] += GAMES_REPEAT
    ch = await _run(session, _cmd_press(CHAT, PETROV, "игры"))
    assert ch.sent == first.sent and ch.toasts == [""]
    clock[0] += timedelta(minutes=1)  # пауза отсчитывается от последнего ответа
    ch = await _run(session, _games_text())
    assert ch.sent == []


async def test_games_changed_schedule_sent_within_pause(session, tenant_cfg, clock):
    _, entries = await _ready(session, tenant_cfg)
    first = await _run(session, _games_text())
    await _rel_match(session, entries["abl"], "g1", days=2)  # расписание поменялось
    clock[0] += timedelta(minutes=1)
    ch = await _run(session, _cmd_press(CHAT, PETROV, "игры"))
    assert len(ch.sent) == 1 and ch.sent[0][1] != first.sent[0][1]
    assert "Соперник g1" in ch.sent[0][1] and ch.toasts == [""]


async def test_games_pause_per_chat(session, tenant_cfg, clock):
    tenant, _ = await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handlers._chat_reply(session, ch, tenant.id, CHAT, "игры")
    await handlers._chat_reply(session, ch, tenant.id, "2000000099", "игры")  # другая беседа — независимо
    assert [p for p, _, _ in ch.sent] == [CHAT, "2000000099"]
    await handlers._chat_reply(session, ch, tenant.id, "2000000099", "игры")
    assert len(ch.sent) == 2


async def test_games_dm_no_pause(session, tenant_cfg, clock):
    await _ready(session, tenant_cfg)
    await _run(session, _games_text())  # в беседе только что ответили
    for _ in range(2):
        ch = await _run(session, IncomingMessage(PETROV, PETROV, "игры", is_chat=False, mentioned=False))
        assert [p for p, _, _ in ch.sent] == [PETROV]


async def test_other_chat_commands_not_paused(session, tenant_cfg, clock):
    await _ready(session, tenant_cfg)
    for _ in range(2):
        ch = await _run(session, _cmd_press(CHAT, CAPTAIN, "помощь"))
        assert [p for p, _, _ in ch.sent] == [CHAT] and ch.toasts == [""]


# ---------- polls.restart ----------


async def _published(session, entry, ch: FakeChannel, days: float = 2):
    m = await _rel_match(session, entry, "g1", days=days)
    chat = (await session.execute(select(Chat))).scalar_one()
    poll = await polls.publish(session, ch, chat, m)
    return m, poll


async def test_restart_resets_votes_and_closes_poll(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    ch = FakeChannel()
    m, poll = await _published(session, entries["abl"], ch)
    petrov, sidorov = await _player(session, tenant, PETROV), await _player(session, tenant, SIDOROV)
    await _vote(session, tenant.id, m, petrov, "going")
    await _vote(session, tenant.id, m, sidorov, "not_going")

    votes = await polls.restart(session, ch, tenant.id, m)
    assert sorted(votes) == sorted([(petrov.id, Vote.GOING), (sidorov.id, Vote.NOT_GOING)])
    assert (await session.scalars(select(Attendance))).all() == []
    await session.refresh(poll)
    assert poll.closed_at is not None and poll.superseded_by_id is None
    ref = MessageRef(CHAT, poll.message_ref)
    assert ch.edits[-1] == (ref, polls.CLOSED_TEXT)
    assert ch.buttons[(CHAT, poll.message_ref)] is None, "кнопок на закрытом анонсе нет"
    assert await polls._current_poll(session, tenant.id, m.id) is None
    assert len(ch.sent) == 1, "новый анонс restart не публикует"


async def test_restart_without_poll_only_resets(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "g1")
    petrov = await _player(session, tenant, PETROV)
    await _vote(session, tenant.id, m, petrov, "going")
    ch = FakeChannel()
    assert await polls.restart(session, ch, tenant.id, m) == [(petrov.id, Vote.GOING)]
    assert (await session.scalars(select(Attendance))).all() == []
    assert ch.sent == [] and ch.edits == []


async def test_restart_old_poll_not_editable_still_closed(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m, poll = await _published(session, entries["abl"], FakeChannel())
    ch = FakeChannel(editable=False)  # VK не даёт править анонс старше суток
    assert await polls.restart(session, ch, tenant.id, m) == []
    await session.refresh(poll)
    assert poll.closed_at is not None and ch.sent == []


async def test_vote_on_closed_poll_toast_no_vote(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    ch = FakeChannel()
    m, poll = await _published(session, entries["abl"], ch)
    await polls.restart(session, ch, tenant.id, m)
    edits = len(ch.edits)
    press = ButtonPress(CHAT, PETROV, MessageRef(CHAT, poll.message_ref), {"m": m.id, "v": "going"}, "e")
    await handle(session, ch, press)
    assert ch.toasts == [polls.CLOSED_TOAST]
    assert (await session.scalars(select(Attendance))).all() == []
    assert len(ch.edits) == edits

    # кнопки «Буду / Не буду» в личке (напоминание, сообщение о переносе) — голос учитывается
    dm = ButtonPress(PETROV, PETROV, MessageRef(PETROV, "777"), {"m": m.id, "v": "going"}, "e2")
    await handle(session, ch, dm)
    assert [a.vote for a in (await session.scalars(select(Attendance))).all()] == [Vote.GOING]


# ---------- полный сценарий: смена времени → новый опрос ----------


async def _setup_week(session, tenant_cfg):
    """Матч сб 12:00 (МСК), анонс в среду, Петров «Буду», Сидоров «Не буду»."""
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    old = (await session.scalars(select(PollMessage))).one()
    petrov, sidorov = await _player(session, tenant, PETROV), await _player(session, tenant, SIDOROV)
    await _vote(session, tenant.id, m, petrov, "going")
    await _vote(session, tenant.id, m, sidorov, "not_going")
    return tenant, m, old


def _change(m, kind=ChangeKind.TIME_CHANGED, **payload) -> ChangeEvent:
    return ChangeEvent(kind=kind, match_id=m.id, payload=json.dumps(payload) if payload else None)


async def test_time_changed_full_scenario(session, tenant_cfg):
    tenant, m, old = await _setup_week(session, tenant_cfg)
    ch0 = FakeChannel()
    await weekly.tick(session, ch0, now=at(FRI, 6))  # T−30 ч: напоминание молчащим
    assert not any(p in (PETROV, SIDOROV) and t.startswith(REMIND) for p, t, _ in ch0.sent)

    m.start_at, m.time_status = naive_utc(at(SAT, 18)), "time_changed"
    session.add(_change(m))
    await session.commit()
    ch = FakeChannel()
    await scheduler.process_changes(session, ch)

    chat = [t for p, t, _ in ch.sent if p == CHAT]
    assert len(chat) == 1 and chat[0].startswith("⏰ Изменилось время матча") and chat[0].endswith(RESET)
    assert "Проверьте свою отметку" not in chat[0]
    dms = {p: (t, b) for p, t, b in ch.sent if p != CHAT}
    assert set(dms) == {PETROV, SIDOROV}
    assert "«Буду» сброшена" in dms[PETROV][0] and AGAIN in dms[PETROV][0]
    assert "«Не буду» сброшена" in dms[SIDOROV][0]
    assert all(b == polls.vote_buttons(m.id) for _, b in dms.values())
    assert (await session.scalars(select(Attendance))).all() == []
    await session.refresh(old)
    assert old.closed_at is not None
    assert ch.edits == [(MessageRef(CHAT, old.message_ref), polls.CLOSED_TEXT)]

    # недельный цикл публикует новый анонс сразу (в тот же час, что и прошлая попытка — новый ключ)
    ch2 = FakeChannel()
    await weekly.tick(session, ch2, now=at(FRI, 6, 1))
    posts = [(t, b) for p, t, b in ch2.sent if p == CHAT]
    assert len(posts) == 1 and posts[0][1] == polls.vote_buttons(m.id)
    assert "18:00" in posts[0][0]
    current = (
        await session.scalars(
            select(PollMessage).where(PollMessage.closed_at.is_(None), PollMessage.superseded_by_id.is_(None))
        )
    ).all()
    assert len(current) == 1 and current[0].id != old.id
    assert ch2.pins == [MessageRef(CHAT, current[0].message_ref)]
    await weekly.tick(session, ch2, now=at(FRI, 6, 2))
    assert len([p for p, _, _ in ch2.sent if p == CHAT]) == 1, "второй анонс не публикуется"

    # новое T−30 ч (пт 12:00): напоминание уходит заново — и тем, чья отметка сброшена
    ch3 = FakeChannel()
    await weekly.tick(session, ch3, now=at(FRI, 12))
    reminded = {p for p, t, _ in ch3.sent if t.startswith(REMIND)}
    assert PETROV in reminded and SIDOROV in reminded


async def test_rescheduled_and_time_changed_one_restart(session, tenant_cfg):
    tenant, m, old = await _setup_week(session, tenant_cfg)
    new_day = date(2026, 10, 25)
    m.match_date, m.start_at, m.status = new_day, naive_utc(at(new_day, 18)), "rescheduled"
    session.add_all([_change(m, ChangeKind.RESCHEDULED, was=SAT.isoformat()), _change(m)])
    await session.commit()
    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    chat = [t for p, t, _ in ch.sent if p == CHAT]
    assert len(chat) == 2
    assert chat[0].startswith("⚠️ Перенос матча") and chat[0].endswith(RESET)
    assert RESET not in chat[1], "второй раз не сбрасываем"
    assert sorted(p for p, _, _ in ch.sent if p != CHAT) == sorted([PETROV, SIDOROV]), "по одной личке"
    assert ch.edits == [(MessageRef(CHAT, old.message_ref), polls.CLOSED_TEXT)]
    assert all(e.processed for e in (await session.scalars(select(ChangeEvent))).all())


async def test_reschedule_far_new_poll_on_poll_day(session, tenant_cfg):
    """Перенесли на неделю: новый анонс — не сразу, а в среду перед новой датой."""
    tenant, m, old = await _setup_week(session, tenant_cfg)
    new_day = date(2026, 10, 31)
    m.match_date, m.start_at, m.status = new_day, naive_utc(at(new_day, 12)), "rescheduled"
    session.add(_change(m, ChangeKind.RESCHEDULED, was=SAT.isoformat()))
    await session.commit()
    await scheduler.process_changes(session, FakeChannel())
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(FRI, 10))
    assert [p for p, _, _ in ch.sent if p == CHAT] == []
    await weekly.tick(session, ch, now=at(date(2026, 10, 28), 10))  # среда перед новой датой
    posts = [t for p, t, _ in ch.sent if p == CHAT]
    assert len(posts) == 1 and "отметьтесь заново" in posts[0], "статус «перенесён» в анонсе"


async def test_manual_time_change_creates_event_and_restarts(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1", hhmm=None)
    await leader.set_manual_time(session, tenant.id, m, "12:00")
    assert (await session.scalars(select(ChangeEvent))).all() == [], "первое время — не изменение"
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    old = (await session.scalars(select(PollMessage))).one()
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV), "going")

    await leader.set_manual_time(session, tenant.id, m, "12:00")  # то же время — без события
    assert (await session.scalars(select(ChangeEvent))).all() == []
    reply = await leader.set_manual_time(session, tenant.id, m, "19:30")
    assert reply.startswith("Время изменено:") and "19:30" in reply
    assert "Опрос перезапускается: прежние отметки сброшены" in reply
    ev = (await session.scalars(select(ChangeEvent))).one()
    assert ev.kind == ChangeKind.TIME_CHANGED and not ev.processed
    assert json.loads(ev.payload)["source"] == "manual"

    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    chat = [t for p, t, _ in ch.sent if p == CHAT]
    assert len(chat) == 1 and "19:30" in chat[0] and chat[0].endswith(RESET)
    assert [p for p, _, _ in ch.sent if p != CHAT] == [PETROV]
    await session.refresh(old)
    assert old.closed_at is not None
    assert (await session.scalars(select(Attendance))).all() == []


async def test_scheduler_tick_processes_manual_change_without_sync(session, tenant_cfg, monkeypatch):
    """process_changes — на каждом тике, даже когда синхронизация не положена (ночь)."""
    import httpx

    tenant, m, old = await _setup_week(session, tenant_cfg)
    monkeypatch.setattr(scheduler, "due_jobs", lambda local: [])
    m.start_at = naive_utc(at(SAT, 19))
    session.add(_change(m, source="manual"))
    await session.commit()
    ch = FakeChannel()
    async with httpx.AsyncClient() as client:
        await scheduler.tick(session, ch, client, at(FRI, 3).astimezone())
    assert any(p == CHAT and t.endswith(RESET) for p, t, _ in ch.sent)
    await session.refresh(old)
    assert old.closed_at is not None


async def test_vote_on_superseded_before_restart_not_counted(session, tenant_cfg):
    """Анонс переопубликовали (старый не редактировался, «кнопки здесь тоже работают»), потом перенос.
    Нажатие на старом сообщении — голос из прежнего опроса, в новый засчитываться не должен."""
    tenant, entries = await _ready(session, tenant_cfg)
    ch = FakeChannel()
    m, first = await _published(session, entries["abl"], ch)
    ch.editable = False
    await polls.refresh(session, ch, tenant.id, m)  # → новый анонс, first.superseded_by_id задан
    await session.refresh(first)
    assert first.superseded_by_id is not None
    await polls.restart(session, ch, tenant.id, m)
    press = ButtonPress(CHAT, PETROV, MessageRef(CHAT, first.message_ref), {"m": m.id, "v": "going"}, "e")
    await handle(session, ch, press)
    assert (await session.scalars(select(Attendance))).all() == []
