"""ADR 0008: тихие часы 22:00–10:00 МСК, скрытие сыгранных игр из анонса, фамилии в анонсе, «@бот игры»
от руководителя — текст текущего анонса. Вымышленные имена."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from basket_assist.bot.handlers import handle
from basket_assist.channels.base import IncomingMessage, MessageRef
from basket_assist.domain.models import MatchStatus
from basket_assist.services import polls, quiet, scheduler, weekly
from basket_assist.storage.tables import Chat, Match, NotificationLog, Player, SeasonRosterEntry, utcnow
from tests.fakes import FakeChannel
from tests.test_digests import _match as _rel_match
from tests.test_leader_bot import PETROV, _ready
from tests.test_roster import CAPTAIN, CHAT, _player
from tests.test_weekly import D2, D24, FRI, SAT, _texts, _to, _vote, _week_match, at

BOT_ROW = [polls.BOT_BUTTON]

# ---------- quiet.is_quiet ----------


@pytest.mark.parametrize(
    ("hh", "mm", "expected"),
    [
        (21, 59, False),
        (22, 0, True),
        (23, 59, True),
        (0, 0, True),
        (3, 0, True),
        (9, 59, True),
        (10, 0, False),
    ],
)
def test_is_quiet_boundaries_msk(hh, mm, expected):
    assert quiet.is_quiet(at(FRI, hh, mm)) is expected
    naive = at(FRI, hh, mm).astimezone(UTC).replace(tzinfo=None)  # naive — считается UTC
    assert quiet.is_quiet(naive) is expected


# ---------- тихие часы в недельном цикле ----------


async def test_poll_when_time_appears_at_night_waits_until_10(session, tenant_cfg):
    """Время появилось ночью — анонс не публикуется до 10:00 МСК."""
    _, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1", hhmm=None)
    m.start_at, m.time_status = at(SAT, 18).astimezone(UTC).replace(tzinfo=None), "time_set"
    await session.commit()
    ch = FakeChannel()
    for now in (at(FRI, 22), at(FRI, 23, 30), at(SAT, 9, 59)):
        await weekly.tick(session, ch, now=now)
    assert _to(ch, CHAT) == [] and ch.pins == []
    await weekly.tick(session, ch, now=at(SAT, 10))
    assert len(_to(ch, CHAT)) == 1 and len(ch.pins) == 1


async def test_digests_not_blocked_by_quiet_hours(session, tenant_cfg):
    """Игра сб 11:00: сводка за 2 ч (сб 09:00) — внутри тихих часов, но уходит вовремя."""
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1", hhmm=(11, 0))
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(FRI, 11))
    assert [t.split("\n", 1)[0] for t in _texts(ch, CAPTAIN, "📋")] == [D24]
    assert quiet.is_quiet(at(SAT, 9))
    await weekly.tick(session, ch, now=at(SAT, 9))
    assert [t.split("\n", 1)[0] for t in _texts(ch, CAPTAIN, "📋")] == [D24, D2]
    assert len(_to(ch, CHAT)) == 1, "анонс опубликован днём (пт 11:00), ночью — ничего нового в беседе"


# ---------- фамилии ----------


async def _roster_player(session, tenant, entry, display: str, last: str, first: str, ext: str) -> Player:
    from basket_assist.services import roster

    captain = await _player(session, tenant, CAPTAIN)
    p = Player(tenant_id=tenant.id, display_name=display)
    r = SeasonRosterEntry(
        league_team_id=entry.league_team_id,
        tournament_id=entry.tournament_id,
        external_person_id=ext,
        last_name=last,
        first_name=first,
    )
    session.add_all([p, r])
    await session.flush()
    await roster.confirm(session, tenant.id, p.id, entry.id, r.id, captain.id)
    await session.commit()
    return p


async def test_surnames_roster_star_and_initials(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    abl = entries["abl"]
    a = await _roster_player(session, tenant, abl, "Тоша", "Иванов", "Антон", "x1")
    b = await _roster_player(session, tenant, abl, "Боря И", "Иванов", "Борис", "x2")
    c = await _roster_player(session, tenant, abl, "Гена", "Фамилькин", "Геннадий", "x3")
    u1 = Player(tenant_id=tenant.id, display_name="Олег Кузьмин")
    u2 = Player(tenant_id=tenant.id, display_name="Пётр Кузьмин")
    u3 = Player(tenant_id=tenant.id, display_name="Иван Иванов")  # не сопоставлен — «Иванов*», не дубль
    session.add_all([u1, u2, u3])
    await session.commit()
    names = await polls.surnames(session, tenant.id, [a, b, c, u1, u2, u3], abl)
    assert names == ["Иванов А.", "Иванов Б.", "Фамилькин", "Кузьмин* О.", "Кузьмин* П.", "Иванов*"]
    # фамилия — из заявки на сезон именно этой лиги: в МЛБЛ тот же игрок не сопоставлен
    assert await polls.surnames(session, tenant.id, [c], entries["mlbl"]) == ["Гена*"]


async def test_surnames_star_only_while_unmatched(session, tenant_cfg):
    """Звёздочка — только «ещё не сопоставлен». Статус разобран («не играет в этой лиге», «нет в заявке
    на сезон») — без пометки: фамилия из заявки другой лиги, а если её нет — из имени в мессенджере."""
    from basket_assist.services import roster

    tenant, entries = await _ready(session, tenant_cfg)
    abl, mlbl = entries["abl"], entries["mlbl"]
    c = await _roster_player(session, tenant, abl, "Гена", "Фамилькин", "Геннадий", "x3")
    u = Player(tenant_id=tenant.id, display_name="Олег Кузьмин")
    session.add(u)
    await session.commit()
    assert await polls.surnames(session, tenant.id, [c, u], mlbl) == ["Гена*", "Кузьмин*"]
    await roster.mark_not_in_league(session, tenant.id, c.id, mlbl.id, c.id)
    await roster.mark_not_in_roster(session, tenant.id, u.id, mlbl.id, c.id)
    await session.commit()
    assert await polls.surnames(session, tenant.id, [c, u], mlbl) == ["Фамилькин", "Кузьмин"]


async def test_announce_names_in_column_without_numbering(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "g1", days=2)
    for vk, vote in ((PETROV, "going"), ("11", "going"), ("13", "not_going")):
        await _vote(session, tenant.id, m, await _player(session, tenant, vk), vote)
    text = await polls.render(session, m, tenant.id)
    assert "\n\n✅ Буду (2):\nПетров\nСидоров\n\n❌ Не буду (1):\nКоманды*\n\n" in text
    assert "1." not in text and "Александр" not in text


# ---------- сыгранная игра пропадает из анонса ----------


def test_shown_hides_two_hours_after_start_and_played():
    now = datetime(2026, 10, 24, 12, 0)
    fresh = Match(id=1, start_at=now - polls.HIDE_AFTER_START + timedelta(seconds=1), status="scheduled")
    old = Match(id=2, start_at=now - polls.HIDE_AFTER_START, status="scheduled")
    played = Match(id=3, start_at=now + timedelta(hours=1), status=MatchStatus.PLAYED)
    no_time = Match(id=4, start_at=None, status="scheduled")
    assert polls.shown([fresh, old, played, no_time], now) == [fresh, no_time]


@pytest.fixture
def poll_now(monkeypatch):
    """Часы polls.utcnow; задаются через poll_now[0] (naive UTC)."""
    now = [utcnow()]
    monkeypatch.setattr(polls, "utcnow", lambda: now[0])
    return now


async def _two_published(session, tenant_cfg, ch):
    tenant, entries = await _ready(session, tenant_cfg)
    m1 = await _rel_match(session, entries["abl"], "a1", days=1)
    m2 = await _rel_match(session, entries["mlbl"], "b1", days=2)
    chat = (await session.execute(select(Chat))).scalar_one()
    p1 = await polls.publish(session, ch, chat, m1)
    await polls.publish(session, ch, chat, m2)
    return tenant, m1, m2, MessageRef(CHAT, p1.message_ref)


async def test_hide_played_removes_game_once(session, tenant_cfg, poll_now):
    ch = FakeChannel()
    tenant, m1, m2, ref = await _two_published(session, tenant_cfg, ch)
    poll_now[0] = m1.start_at + polls.HIDE_AFTER_START - timedelta(minutes=1)
    assert await polls.hide_played(session, ch) == 0
    edits = len(ch.edits)

    poll_now[0] = m1.start_at + polls.HIDE_AFTER_START
    assert await polls.hide_played(session, ch) == 1
    assert len(ch.edits) == edits + 1 and ch.edits[-1][0] == ref, "правка, без нового сообщения"
    text = ch.messages[(CHAT, ref.message_id)]
    assert "🏀 АБЛ:" not in text and "🏀 МЛБЛ:" in text
    assert ch.buttons[(CHAT, ref.message_id)] == [*polls.vote_buttons(m2.id), BOT_ROW]
    logged = (await session.scalars(select(NotificationLog).filter_by(kind="hide_played"))).all()
    assert [r.dedup_key for r in logged] == [str(m1.id)]

    sent = len(ch.sent)
    assert await polls.hide_played(session, ch) == 0, "раз на матч"
    assert len(ch.edits) == edits + 1 and len(ch.sent) == sent


async def test_all_played_announce_without_buttons(session, tenant_cfg, poll_now):
    ch = FakeChannel()
    tenant, m1, m2, ref = await _two_published(session, tenant_cfg, ch)
    pins, sent = list(ch.pins), len(ch.sent)
    poll_now[0] = m2.start_at + polls.HIDE_AFTER_START
    assert await polls.hide_played(session, ch) == 1, "общий анонс правится один раз"
    assert ch.messages[(CHAT, ref.message_id)] == polls.ALL_PLAYED
    assert ch.buttons[(CHAT, ref.message_id)] is None
    assert len(ch.sent) == sent and ch.pins == pins
    assert await polls.week_text(session, ch, tenant.id) is None


async def test_all_played_edit_failed_not_republished(session, tenant_cfg, poll_now):
    ch = FakeChannel()
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "a1", days=1)
    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, ch, chat, m)
    ch.editable = False
    sent, pins = len(ch.sent), list(ch.pins)
    poll_now[0] = m.start_at + polls.HIDE_AFTER_START
    await polls.hide_played(session, ch)
    assert len(ch.sent) == sent and ch.pins == pins, "«игры сыграны» не переопубликовывается"


async def test_scheduler_tick_hides_played_even_at_night(session, tenant_cfg, poll_now, monkeypatch):
    monkeypatch.setattr(scheduler, "due_jobs", lambda local: [])
    ch = FakeChannel()
    _, m1, _, ref = await _two_published(session, tenant_cfg, ch)
    poll_now[0] = m1.start_at + polls.HIDE_AFTER_START
    night = at(FRI, 3)
    assert quiet.is_quiet(night)
    async with httpx.AsyncClient() as client:
        await scheduler.tick(session, ch, client, night)
        edits = len(ch.edits)
        await scheduler.tick(session, ch, client, night + timedelta(minutes=1))
    assert "🏀 АБЛ:" not in ch.messages[(CHAT, ref.message_id)]
    assert len(ch.edits) == edits, "раз на матч"


# ---------- «@бот игры» от руководителя — текст анонса ----------


async def test_leader_games_in_chat_shows_week_announce(session, tenant_cfg, poll_now):
    ch = FakeChannel()
    tenant, m1, m2, ref = await _two_published(session, tenant_cfg, ch)
    await _vote(session, tenant.id, m2, await _player(session, tenant, PETROV))
    await polls.refresh(session, ch, tenant.id, m2)
    out = FakeChannel()
    await handle(session, out, IncomingMessage(CHAT, CAPTAIN, "игры", is_chat=True, mentioned=True))
    [(peer, text, buttons)] = out.sent
    assert peer == CHAT and buttons is None
    assert text == ch.messages[(CHAT, ref.message_id)] and "✅ Буду (1):\nПетров" in text

    # первая игра сыграна (2 ч после начала) — её в ответе нет
    poll_now[0] = m1.start_at + polls.HIDE_AFTER_START
    text = await polls.week_text(session, ch, tenant.id)
    assert "🏀 АБЛ:" not in text and "🏀 МЛБЛ:" in text
