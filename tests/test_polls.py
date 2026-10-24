"""Анонс-голосование: публикация, нажатия, смена ответа, переопубликация, приватность беседы."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from basket_assist.bot.handlers import UNKNOWN_CHAT, handle
from basket_assist.channels.base import ButtonPress, IncomingMessage, MessageRef
from basket_assist.services import notify, polls
from basket_assist.storage.bootstrap import apply_tenant_config
from basket_assist.storage.tables import (
    Attendance,
    Base,
    Chat,
    League,
    LeagueTeam,
    Match,
    PollMessage,
    Tournament,
    utcnow,
)
from tests.fakes import FakeChannel

CHAT = "2000000001"  # из config/tenant.example.toml


async def _setup(session, tenant_cfg, start_in=timedelta(days=3)):
    await apply_tenant_config(session, tenant_cfg)
    league = (await session.execute(select(League).filter_by(key="abl"))).scalar_one()
    tournament = (await session.execute(select(Tournament).filter_by(league_id=league.id))).scalar_one()
    ours = (
        await session.execute(select(LeagueTeam).filter_by(league_id=league.id, external_id="279"))
    ).scalar_one()
    rival = LeagueTeam(league_id=league.id, external_id="2301", name="Pokrasote")
    session.add(rival)
    await session.flush()
    start = utcnow() + start_in
    m = Match(
        tournament_id=tournament.id,
        external_id="1",
        home_team_id=ours.id,
        away_team_id=rival.id,
        match_date=start.date(),
        start_at=start,
        time_status="time_set",
        status="scheduled",
        venue_name="Шелепиха",
    )
    session.add(m)
    await session.commit()
    chat = (await session.execute(select(Chat))).scalar_one()
    return chat, m


def press(match_id: int, user: str, vote: str, cmid: str = "101") -> ButtonPress:
    return ButtonPress(CHAT, user, MessageRef(CHAT, cmid), {"m": match_id, "v": vote}, event_id="e")


async def test_publish_render_and_pin(session, tenant_cfg):
    chat, m = await _setup(session, tenant_cfg)
    ch = FakeChannel()
    poll = await polls.publish(session, ch, chat, m)
    assert poll.message_ref == "101" and ch.pins == [MessageRef(CHAT, "101")]
    text = ch.sent[0][1]
    assert "АБЛ: Westchester — Pokrasote" in text and "Шелепиха" in text
    assert "✅ Буду (0):\n—\n\n❌ Не буду (0):\n—" in text
    assert "Под вопросом" not in text
    assert [b.label for b in ch.sent[0][2][0]] == ["Буду", "Не буду"]
    assert ch.sent[0][2] == [*polls.vote_buttons(m.id), [polls.BOT_BUTTON]], (
        "ADR 0008: «Бот в личке» последним"
    )
    # повторная публикация не дублирует анонс
    await polls.publish(session, ch, chat, m)
    assert len(ch.sent) == 1


async def test_votes_change_and_names(session, tenant_cfg):
    chat, m = await _setup(session, tenant_cfg)
    ch = FakeChannel(names={"10": "Иван Петров", "11": "Олег Сидоров"})
    await polls.publish(session, ch, chat, m)

    await handle(session, ch, press(m.id, "10", "going"))
    await handle(session, ch, press(m.id, "11", "going"))
    await handle(session, ch, press(m.id, "10", "not_going"))  # передумал
    text = ch.messages[(CHAT, "101")]
    # ADR 0008: фамилии в столбик; не сопоставлен с заявкой на сезон — фамилия из VK со звёздочкой
    assert "✅ Буду (1):\nСидоров*\n\n❌ Не буду (1):\nПетров*\n" in text
    assert "Олег" not in text and "Иван" not in text
    assert ch.toasts == ["Отмечено: Буду", "Отмечено: Буду", "Отмечено: Не буду"]

    edits_before = len(ch.edits)
    await handle(session, ch, press(m.id, "10", "not_going"))  # повторное нажатие — без изменений
    assert len(ch.edits) == edits_before
    assert len((await session.scalars(select(Attendance))).all()) == 2


@pytest.mark.parametrize("deletable", [True, False], ids=["deleted", "not_deleted"])
async def test_republish_when_edit_fails(session, tenant_cfg, deletable):
    """VK не дал отредактировать анонс: новый анонс + закрепление, старый удаляется для всех;
    не удалось удалить — пометка «устарел» без кнопок (ADR 0007, «Дополнение»)."""
    chat, m = await _setup(session, tenant_cfg)
    ch = FakeChannel(names={"10": "Иван Петров"})
    ch.deletable = deletable
    await polls.publish(session, ch, chat, m)
    ch.editable = False  # VK не даёт отредактировать старый анонс (ошибка 909)

    await handle(session, ch, press(m.id, "10", "going"))
    assert len(ch.sent) == 2 and "✅ Буду (1):\nПетров*\n" in ch.sent[1][1]
    assert ch.pins[-1] == MessageRef(CHAT, "102")
    current = (await session.scalars(select(PollMessage).where(PollMessage.superseded_by_id.is_(None)))).one()
    assert current.message_ref == "102"
    old = MessageRef(CHAT, "101")
    if deletable:
        assert ch.deleted == [old] and (CHAT, "101") not in ch.messages
        assert all(ref != old for ref, _ in ch.edits), "удалённое сообщение не редактируется"
    else:
        assert ch.deleted == []
        assert ch.messages[(CHAT, "101")] == polls.STALE_TEXT
        assert ch.buttons[(CHAT, "101")] is None, "на устаревшем анонсе кнопок нет"


async def test_voting_closed_after_start(session, tenant_cfg):
    chat, m = await _setup(session, tenant_cfg, start_in=timedelta(minutes=-5))
    ch = FakeChannel()
    await handle(session, ch, press(m.id, "10", "going"))
    assert ch.toasts == ["Голосование закрыто — матч уже начался"]
    assert (await session.scalars(select(Attendance))).all() == []


async def test_chat_messages_without_mention_are_ignored(session, tenant_cfg):
    await _setup(session, tenant_cfg)
    ch = FakeChannel()
    await handle(
        session, ch, IncomingMessage(CHAT, "10", "обсуждаем тренировку", is_chat=True, mentioned=False)
    )
    assert ch.sent == []
    # ADR 0008: на «игры» в беседу отвечает только руководителю/администратору; остальным — ссылка на личку
    await handle(session, ch, IncomingMessage(CHAT, "10", "игры", is_chat=True, mentioned=True))
    assert ch.sent[0][0] == CHAT and "vk.me" in ch.sent[0][1] and "Pokrasote" not in ch.sent[0][1]
    notify.set_admin("10")
    try:
        await handle(session, ch, IncomingMessage(CHAT, "10", "игры", is_chat=True, mentioned=True))
    finally:
        notify.set_admin(None)
    assert ch.sent[1][0] == CHAT and "Westchester — Pokrasote" in ch.sent[1][1]


UNKNOWN_PEER = "2000000099"  # беседа, не подключённая ни к одной команде


async def _row_counts(session) -> dict[str, int]:
    """Число строк в каждой таблице — чтобы проверить, что ничего не сохранено."""
    return {
        t.name: (await session.execute(select(func.count()).select_from(t))).scalar_one()
        for t in Base.metadata.sorted_tables
    }


async def test_unknown_chat_mention_gets_one_reply_and_nothing_saved(session, tenant_cfg):
    """Обращение из неподключённой беседы — ровно один ответ UNKNOWN_CHAT без кнопок, в БД ничего."""
    await _setup(session, tenant_cfg)
    before = await _row_counts(session)
    ch = FakeChannel(names={"77": "Иван Иванов"})
    await handle(session, ch, IncomingMessage(UNKNOWN_PEER, "77", "игры", is_chat=True, mentioned=True))
    assert ch.sent == [(UNKNOWN_PEER, UNKNOWN_CHAT, None)]
    assert ch.edits == [] and ch.pins == []
    assert await _row_counts(session) == before


async def test_unknown_chat_without_mention_is_silent(session, tenant_cfg):
    await _setup(session, tenant_cfg)
    before = await _row_counts(session)
    ch = FakeChannel()
    for text in ("игры", "обсуждаем тренировку"):
        await handle(session, ch, IncomingMessage(UNKNOWN_PEER, "10", text, is_chat=True, mentioned=False))
    await handle(
        session,
        ch,
        IncomingMessage(UNKNOWN_PEER, "77", "", is_chat=True, mentioned=False, action="chat_invite_user"),
    )
    assert ch.sent == [] and ch.toasts == []
    assert await _row_counts(session) == before


async def test_unknown_chat_buttons_are_ignored(session, tenant_cfg):
    """Кнопки в неподключённой беседе игнорируются — даже от известного сервису игрока (капитана)."""
    chat, m = await _setup(session, tenant_cfg)
    before = await _row_counts(session)
    ch = FakeChannel()
    for user in ("77", "1"):  # незнакомый и капитан из config/tenant.example.toml
        for payload in ({"m": m.id, "v": "going"}, {"c": "игры"}, {"c": "помощь"}, {"a": "gm"}):
            await handle(
                session,
                ch,
                ButtonPress(UNKNOWN_PEER, user, MessageRef(UNKNOWN_PEER, "5"), payload, event_id="e"),
            )
    assert ch.sent == [] and ch.toasts == []
    assert await _row_counts(session) == before


async def test_start_in_dm_enables_dm(session, tenant_cfg):
    await _setup(session, tenant_cfg)
    ch = FakeChannel(names={"10": "Иван Петров"})
    await handle(
        session,
        ch,
        IncomingMessage(
            "10", "10", "Начать", is_chat=False, mentioned=False, payload={"command": "start"}, ref="chat"
        ),
    )
    assert "подключены" in ch.sent[0][1]
    from basket_assist.storage.tables import MessengerAccount

    acc = (await session.execute(select(MessengerAccount).filter_by(external_user_id="10"))).scalar_one()
    assert acc.dm_allowed is True and acc.connected_at is not None


async def test_render_without_time(session, tenant_cfg):
    chat, m = await _setup(session, tenant_cfg)
    m.start_at, m.time_status = None, "date_only"
    await session.commit()
    text = await polls.render(session, m, chat.tenant_id)
    assert "время пока не назначено" in text
