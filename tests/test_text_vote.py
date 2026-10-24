"""Запасной путь, если кнопки анонса не работают (24.10): отметка текстом в личке, самопроверка анонсов
(пропали кнопки — переопубликовать, сняли закреп — закрепить один раз), сигналы администратору сервиса.
Вымышленные имена."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from basket_assist.bot.handlers import UNKNOWN, handle
from basket_assist.channels.base import ChannelError, IncomingMessage, MessageRef
from basket_assist.services import notify, polls
from basket_assist.storage.tables import Attendance, Chat, NotificationLog, PollMessage, utcnow
from tests.fakes import FakeChannel
from tests.test_digests import _match as _rel_match
from tests.test_leader_bot import PETROV, SIDOROV, _ready
from tests.test_roster import CAPTAIN, CHAT

ADMIN = "99"
FRIEND = "13"  # «Друг Команды» — не сопоставлен с заявкой на сезон


async def _chat(session) -> Chat:
    return (await session.execute(select(Chat).filter_by(peer_id=CHAT))).scalar_one()


async def _dm(session, ch: FakeChannel, user: str, text: str) -> str:
    await handle(session, ch, IncomingMessage(user, user, text, is_chat=False, mentioned=False))
    return [t for p, t, _ in ch.sent if p == user][-1]


async def _votes(session) -> list[tuple[int, str]]:
    return [(a.match_id, a.vote) for a in (await session.scalars(select(Attendance))).all()]


def _admin_msgs(ch: FakeChannel) -> list[str]:
    return [t for p, t, _ in ch.sent if p == ADMIN]


async def _published(session, tenant_cfg, ch: FakeChannel, *specs: tuple[str, str, float]):
    """Матчи (лига, ext, через сколько дней) опубликованы в беседе — в общий анонс, если неделя та же."""
    tenant, entries = await _ready(session, tenant_cfg)
    chat = await _chat(session)
    ms, polls_ = [], []
    for league, ext, days in specs:
        m = await _rel_match(session, entries[league], ext, days=days)
        ms.append(m)
        polls_.append(await polls.publish(session, ch, chat, m))
    return tenant, chat, ms, polls_


# ---------- text_vote: один матч ----------


@pytest.mark.parametrize("text", ["Буду", "буду!", "  БУДУ  ", "буду."])
async def test_single_match_text_vote_going(session, tenant_cfg, text):
    ch = FakeChannel()
    notify.set_admin(ADMIN)
    _, _, (m,), (p,) = await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    reply = await _dm(session, ch, PETROV, text)
    assert reply.startswith("Отмечено: Буду — АБЛ, ") and f"{m.match_date:%d.%m}" in reply
    assert await _votes(session) == [(m.id, "going")]
    # анонс перерисован — как после кнопки
    assert ch.edits[-1][0] == MessageRef(CHAT, p.message_ref)
    assert "Буду (1)" in ch.messages[(CHAT, p.message_ref)]
    assert len(_admin_msgs(ch)) == 1 and "отметился текстом в личке" in _admin_msgs(ch)[0]


@pytest.mark.parametrize("text", ["не буду", "Не  будУ", "НЕ БУДУ!", "небуду"])
async def test_single_match_text_vote_not_going(session, tenant_cfg, text):
    ch = FakeChannel()
    _, _, (m,), _ = await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    reply = await _dm(session, ch, PETROV, text)
    assert reply.startswith("Отмечено: Не буду — АБЛ")
    assert await _votes(session) == [(m.id, "not_going")]


async def test_text_vote_change_and_admin_signal_once(session, tenant_cfg):
    ch = FakeChannel()
    notify.set_admin(ADMIN)
    _, _, (m,), _ = await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    await _dm(session, ch, PETROV, "буду")
    await _dm(session, ch, PETROV, "не буду")
    await _dm(session, ch, PETROV, "буду")
    assert await _votes(session) == [(m.id, "going")]
    assert len(_admin_msgs(ch)) == 1, "администратору — один раз на (матч, игрок)"
    await _dm(session, ch, SIDOROV, "буду")
    assert len(_admin_msgs(ch)) == 2, "другой игрок — отдельный сигнал"
    logs = (await session.scalars(select(NotificationLog).filter_by(kind="text_vote"))).all()
    assert len(logs) == 2


async def test_text_vote_without_admin_only_logs(session, tenant_cfg):
    ch = FakeChannel()
    _, _, (m,), _ = await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    sent_before = len(ch.sent)
    reply = await _dm(session, ch, PETROV, "буду")
    assert reply.startswith("Отмечено: Буду")
    assert await _votes(session) == [(m.id, "going")]
    assert [p for p, _, _ in ch.sent[sent_before:]] == [PETROV], "кроме ответа игроку — ничего"


async def test_text_vote_not_in_roster_signal(session, tenant_cfg):
    """«Буду» текстом — тот же сигнал «не сопоставлен с заявкой на сезон», что и кнопкой."""
    ch = FakeChannel()
    await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    await _dm(session, ch, FRIEND, "буду")
    to_friend = [t for p, t, _ in ch.sent if p == FRIEND]
    assert any("не сопоставлен с заявкой на сезон" in t for t in to_friend)
    assert any("не сопоставлен" in t for p, t, _ in ch.sent if p == CAPTAIN)


async def test_no_open_games(session, tenant_cfg):
    ch = FakeChannel()
    await _ready(session, tenant_cfg)
    assert (await _dm(session, ch, PETROV, "буду")).startswith("Сейчас нет игр")
    assert await _votes(session) == []


async def test_started_match_not_votable(session, tenant_cfg):
    ch = FakeChannel()
    _, _, (m,), _ = await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    m.start_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    assert (await _dm(session, ch, PETROV, "буду")).startswith("Сейчас нет игр")
    assert await _votes(session) == []


# ---------- text_vote: несколько игр ----------


async def test_two_leagues_need_clarification(session, tenant_cfg):
    ch = FakeChannel()
    await _published(session, tenant_cfg, ch, ("abl", "a1", 2), ("mlbl", "b1", 3))
    reply = await _dm(session, ch, PETROV, "буду")
    assert reply.startswith("Игр несколько") and "«буду АБЛ»" in reply and "Варианты: АБЛ, МЛБЛ." in reply
    reply = await _dm(session, ch, PETROV, "не буду")
    assert "«не буду АБЛ»" in reply
    assert await _votes(session) == [], "без уточнения отметка не пишется"


@pytest.mark.parametrize(
    ("text", "idx", "vote"),
    [
        ("буду МЛБЛ", 1, "going"),
        ("Буду на млбл", 1, "going"),
        ("не буду абл", 0, "not_going"),
        ("не буду 2", 1, "not_going"),
        ("буду 1", 0, "going"),
        ("буду DATE1", 1, "going"),
    ],
)
async def test_two_leagues_with_tag(session, tenant_cfg, text, idx, vote):
    ch = FakeChannel()
    _, _, ms, (p, _) = await _published(session, tenant_cfg, ch, ("abl", "a1", 2), ("mlbl", "b1", 3))
    text = text.replace("DATE1", f"{ms[1].match_date:%d.%m}")
    reply = await _dm(session, ch, PETROV, text)
    assert reply.startswith("Отмечено:"), reply
    assert await _votes(session) == [(ms[idx].id, vote)]
    assert ch.edits[-1][0] == MessageRef(CHAT, p.message_ref)


@pytest.mark.parametrize("tag", ["вхл", "3", "01.01"])
async def test_unknown_tag(session, tenant_cfg, tag):
    ch = FakeChannel()
    await _published(session, tenant_cfg, ch, ("abl", "a1", 2), ("mlbl", "b1", 3))
    reply = await _dm(session, ch, PETROV, f"буду {tag}")
    assert reply == f"Не нашёл игру «{tag}». Варианты: АБЛ, МЛБЛ."
    assert await _votes(session) == []


async def test_same_league_tagged_by_date(session, tenant_cfg):
    ch = FakeChannel()
    _, _, ms, _ = await _published(session, tenant_cfg, ch, ("abl", "a1", 1), ("abl", "a2", 4))
    d1, d2 = (f"{m.match_date:%d.%m}" for m in ms)
    reply = await _dm(session, ch, PETROV, "буду")
    assert f"Варианты: {d1}, {d2}." in reply
    assert (await _dm(session, ch, PETROV, "буду абл")).startswith("Не нашёл игру «абл»")
    assert (await _dm(session, ch, PETROV, f"не буду {d2}")).startswith("Отмечено: Не буду")
    assert await _votes(session) == [(ms[1].id, "not_going")]


async def test_buduu_rad_is_unknown_game_not_vote(session, tenant_cfg):
    """«буду рад» распознаётся как отметка с меткой «рад» → «Не нашёл игру» (поведение зафиксировано:
    отметка не пишется, но ответ — не UNKNOWN)."""
    ch = FakeChannel()
    await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    reply = await _dm(session, ch, PETROV, "Буду рад!")
    assert reply == "Не нашёл игру «рад». Варианты: АБЛ."
    assert await _votes(session) == []


@pytest.mark.parametrize("text", ["будущее", "будут все?", "небудет", "я буду", "привет"])
async def test_not_a_vote(session, tenant_cfg, text):
    ch = FakeChannel()
    await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    assert await _dm(session, ch, PETROV, text) == UNKNOWN
    assert await _votes(session) == []


async def test_games_command_still_works(session, tenant_cfg):
    ch = FakeChannel()
    await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    reply = await _dm(session, ch, PETROV, "игры")
    assert not reply.startswith("Отмечено") and reply != UNKNOWN
    assert await _votes(session) == []


async def test_text_vote_function_returns_none_for_non_vote(session, tenant_cfg):
    ch = FakeChannel()
    tenant, _, _, _ = await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    from tests.test_roster import _player

    petrov = await _player(session, tenant, PETROV)
    for text in ("игры", "будущее", "", "помощь"):
        assert await polls.text_vote(session, ch, tenant.id, petrov, text) is None


# ---------- подсказка в анонсе: метки как на кнопках ----------


def _hint_tags(text: str) -> list[str]:
    last = text.rsplit("\n\n", 1)[-1]
    assert last.startswith("Кнопка не сработала? Напишите боту в личку")
    return [part.split("»")[0] for part in last.split("«буду ")[1:] + last.split("«не буду ")[1:]]


async def test_single_announce_ends_with_hint(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "a1", days=2)
    assert (await polls.render(session, m, tenant.id)).endswith("\n\n" + polls.TEXT_VOTE_HINT)


async def test_combined_hint_matches_buttons(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m1 = await _rel_match(session, entries["abl"], "a1", days=2)
    m2 = await _rel_match(session, entries["mlbl"], "b1", days=3)
    text = await polls.render_announce(session, tenant.id, [m1, m2])
    assert text.endswith("например: «буду АБЛ» или «не буду МЛБЛ».")
    labels = {b.label for row in await polls.announce_buttons(session, [m1, m2]) for b in row}
    assert {f"Буду {t}" for t in _hint_tags(text)} <= labels


async def test_numbered_hint_matches_buttons_and_text_vote(session, tenant_cfg):
    ch = FakeChannel()
    tenant, _, ms, _ = await _published(
        session, tenant_cfg, ch, *[("abl", f"a{i}", 1 + i * 0.5) for i in range(5)]
    )
    assert len({p.message_ref for p in (await session.scalars(select(PollMessage))).all()}) == 1
    text = await polls.render_announce(session, tenant.id, ms)
    assert text.endswith("например: «буду 1» или «не буду 5».")
    labels = {b.label for row in await polls.announce_buttons(session, ms) for b in row}
    assert {"Буду 1", "Не буду 5"} <= labels
    assert (await _dm(session, ch, PETROV, "не буду 5")).startswith("Отмечено: Не буду")
    assert await _votes(session) == [(ms[4].id, "not_going")]


async def test_numbered_text_vote_after_first_game_started(session, tenant_cfg):
    ch = FakeChannel()
    tenant, _, ms, _ = await _published(
        session, tenant_cfg, ch, *[("abl", f"a{i}", 1 + i * 0.5) for i in range(5)]
    )
    ms[0].start_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    labels = {b.label for row in await polls.announce_buttons(session, ms) for b in row}
    assert "Буду 2" in labels and "Буду 1" not in labels  # кнопки сохраняют нумерацию анонса
    await _dm(session, ch, PETROV, "буду 2")
    assert await _votes(session) == [(ms[1].id, "going")]


# ---------- notify.admin ----------


async def test_admin_not_set_returns_false():
    ch = FakeChannel()
    assert await notify.admin(ch, "сигнал") is False
    assert ch.sent == []


async def test_admin_sends_dm():
    ch = FakeChannel()
    notify.set_admin(ADMIN)
    assert await notify.admin(ch, "сигнал") is True
    assert ch.sent == [(ADMIN, "сигнал", None)]


async def test_admin_channel_error_is_false():
    class Failing(FakeChannel):
        async def send(self, peer_id, text, buttons=None):
            raise ChannelError(901, "личка закрыта")

    notify.set_admin(ADMIN)
    assert await notify.admin(Failing(), "сигнал") is False


# ---------- check_announces ----------


async def test_check_ok_does_nothing(session, tenant_cfg):
    ch = FakeChannel()
    notify.set_admin(ADMIN)
    await _published(session, tenant_cfg, ch, ("abl", "a1", 2), ("mlbl", "b1", 3))
    sent, pins = len(ch.sent), len(ch.pins)
    assert await polls.check_announces(session, ch) == 0
    assert (len(ch.sent), len(ch.pins)) == (sent, pins)


async def test_check_lost_buttons_republishes(session, tenant_cfg):
    ch = FakeChannel()
    notify.set_admin(ADMIN)
    tenant, _, ms, (p1, p2) = await _published(session, tenant_cfg, ch, ("abl", "a1", 2), ("mlbl", "b1", 3))
    await _dm(session, ch, PETROV, "буду абл")
    old_ref = p1.message_ref
    ch.lost_buttons.add((CHAT, old_ref))
    admin_before = len(_admin_msgs(ch))

    assert await polls.check_announces(session, ch) == 1

    current = (await session.scalars(select(PollMessage).filter_by(superseded_by_id=None))).all()
    assert {p.match_id for p in current} == {ms[0].id, ms[1].id}
    new_ref = current[0].message_ref
    assert new_ref != old_ref and {p.message_ref for p in current} == {new_ref}
    assert ch.pins[-1] == MessageRef(CHAT, new_ref)
    assert MessageRef(CHAT, old_ref) in ch.deleted
    assert ch.buttons[(CHAT, new_ref)], "у нового анонса есть кнопки"
    assert "Буду (1)" in ch.messages[(CHAT, new_ref)], "отметки сохранены"
    assert await _votes(session) == [(ms[0].id, "going")]
    admin = _admin_msgs(ch)[admin_before:]
    assert len(admin) == 1 and "пропали кнопки" in admin[0]
    # новый анонс в порядке — повторная проверка ничего не делает
    assert await polls.check_announces(session, ch) == 0


async def test_check_buttons_unknown_does_nothing(session, tenant_cfg):
    ch = FakeChannel()
    _, _, _, (p,) = await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    ch.lost_buttons.add((CHAT, p.message_ref))
    ch.buttons_unknown = True
    sent = len(ch.sent)
    assert await polls.check_announces(session, ch) == 0
    assert len(ch.sent) == sent and ch.deleted == []


async def test_check_closed_announce_untouched(session, tenant_cfg):
    """Голосование закончилось (игра началась) — кнопок и не должно быть, анонс не трогаем."""
    ch = FakeChannel()
    _, _, (m,), (p,) = await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    m.start_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    ch.lost_buttons.add((CHAT, p.message_ref))
    ch.pinned_override[CHAT] = "777"
    sent, pins = len(ch.sent), len(ch.pins)
    assert await polls.check_announces(session, ch) == 0
    assert (len(ch.sent), len(ch.pins)) == (sent, pins)


async def test_check_repins_once(session, tenant_cfg):
    ch = FakeChannel()
    notify.set_admin(ADMIN)
    _, _, _, (p,) = await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    ch.pinned_override[CHAT] = "777"  # участник закрепил своё
    pins = len(ch.pins)
    assert await polls.check_announces(session, ch) == 1
    assert ch.pins[pins:] == [MessageRef(CHAT, p.message_ref)]
    assert any("откреплён" in t for t in _admin_msgs(ch))
    # закрепили своё снова — не спорим
    assert await polls.check_announces(session, ch) == 0
    assert len(ch.pins) == pins + 1
    logs = (await session.scalars(select(NotificationLog).filter_by(kind="repin"))).all()
    assert len(logs) == 1


async def test_check_nothing_pinned_repins(session, tenant_cfg):
    ch = FakeChannel()
    _, _, _, (p,) = await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    ch.pinned_override[CHAT] = ""  # закреп снят
    assert await polls.check_announces(session, ch) == 1
    assert ch.pins[-1] == MessageRef(CHAT, p.message_ref)


async def test_check_pinned_unknown_does_nothing(session, tenant_cfg):
    ch = FakeChannel()
    await _published(session, tenant_cfg, ch, ("abl", "a1", 2))
    ch.pinned_override[CHAT] = None
    pins = len(ch.pins)
    assert await polls.check_announces(session, ch) == 0
    assert len(ch.pins) == pins


async def test_check_only_latest_announce_repinned(session, tenant_cfg):
    """Два анонса (разные недели): закреплён должен быть последний; старый не перезакрепляется."""
    ch = FakeChannel()
    _, _, _, (p1, p2) = await _published(session, tenant_cfg, ch, ("abl", "a1", 2), ("mlbl", "b1", 10))
    assert p1.message_ref != p2.message_ref
    assert await polls.check_announces(session, ch) == 0  # закреплён последний
    ch.pinned_override[CHAT] = p1.message_ref
    assert await polls.check_announces(session, ch) == 1
    assert ch.pins[-1] == MessageRef(CHAT, p2.message_ref)


# ---------- scheduler.run: самопроверка раз в CHECK_EVERY ----------


class _Stop(Exception):
    pass


async def test_scheduler_run_checks_announces_hourly(monkeypatch):
    from datetime import datetime

    from basket_assist.services import scheduler, weekly

    clock = [datetime(2026, 10, 24, 9, 0)]
    checks: list[datetime] = []
    ticks = [0]

    async def noop(*a, **k):
        return None

    async def check(session, channel):
        checks.append(clock[0])
        if len(checks) == 1:
            raise RuntimeError("сбой самопроверки")  # не должен останавливать цикл
        return 0

    async def sleep(_seconds):
        ticks[0] += 1
        if ticks[0] >= 150:
            raise _Stop
        clock[0] += timedelta(minutes=1)

    class _Sessions:
        def __call__(self):
            return self

        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(scheduler, "refresh_active_polls", noop)
    monkeypatch.setattr(scheduler, "tick", noop)
    monkeypatch.setattr(weekly, "tick", noop)
    monkeypatch.setattr(polls, "check_announces", check)
    monkeypatch.setattr(scheduler, "utcnow", lambda: clock[0])
    monkeypatch.setattr(scheduler.asyncio, "sleep", sleep)
    with pytest.raises(_Stop):
        await scheduler.run(_Sessions(), FakeChannel(), None)
    # 150 минут: проверка сразу при запуске, через час и через два
    assert [c - checks[0] for c in checks] == [timedelta(0), timedelta(hours=1), timedelta(hours=2)]
