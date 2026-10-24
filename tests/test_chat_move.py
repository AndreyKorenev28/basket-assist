"""Переезд беседы: одна активная беседа на команду и канал, перенос открытых анонсов, рассылки в активную,
бот молчит в прежней беседе (подготовка к пилоту, CLAUDE.md «Следующие шаги»). Вымышленные имена и ID."""

import contextlib
from datetime import timedelta

from sqlalchemy import func, select

from basket_assist import app
from basket_assist.bot import leader
from basket_assist.bot.handlers import MOVED_CHAT, MOVED_CHAT_TOAST, UNKNOWN_CHAT, handle
from basket_assist.channels.base import ButtonPress, ChannelError, IncomingMessage, MessageRef
from basket_assist.config import Settings
from basket_assist.domain.models import ChangeKind
from basket_assist.services import chats, polls, scheduler, weekly
from basket_assist.storage.bootstrap import apply_tenant_config
from basket_assist.storage.tables import (
    Attendance,
    ChangeEvent,
    Chat,
    MessengerAccount,
    PollMessage,
    Tenant,
    utcnow,
)
from tests.fakes import FakeChannel
from tests.test_application import _abl as _abl_poll
from tests.test_application import _after_poll, _close_dm
from tests.test_digests import _match as _rel_match
from tests.test_leader_bot import PETROV, SIDOROV, _ready
from tests.test_polls import _row_counts
from tests.test_roster import CAPTAIN, CHAT, _player
from tests.test_weekly import naive_utc

NEW = "2000000002"  # беседа, в которую переезжает команда
UNKNOWN_PEER = "2000000099"


async def _move(session, tenant_cfg, peer: str = NEW):
    """Смена vk_chat_peer_id в конфиге и перезапуск bootstrap (как при выкладке на сервер)."""
    return await apply_tenant_config(session, tenant_cfg.model_copy(update={"vk_chat_peer_id": int(peer)}))


async def _chat(session, peer: str) -> Chat:
    return (await session.execute(select(Chat).filter_by(peer_id=peer))).scalar_one()


async def _active_peers(session) -> set[str]:
    return set((await session.scalars(select(Chat.peer_id).filter_by(active=True))).all())


async def _current(session) -> list[PollMessage]:
    return list((await session.scalars(select(PollMessage).filter_by(superseded_by_id=None))).all())


def _to(ch: FakeChannel, peer: str) -> list[str]:
    return [t for p, t, _ in ch.sent if p == peer]


def _press(peer: str, user: str, payload: dict, cmid: str = "101") -> ButtonPress:
    return ButtonPress(peer, user, MessageRef(peer, cmid), payload, event_id="e")


# ---------- bootstrap и active_chat ----------


async def test_bootstrap_switches_active_chat(session, tenant_cfg):
    tenant = await apply_tenant_config(session, tenant_cfg)
    assert await _active_peers(session) == {CHAT}
    assert (await chats.active_chat(session, tenant.id, "vk")).peer_id == CHAT

    await _move(session, tenant_cfg)
    assert (await session.scalar(select(func.count()).select_from(Chat))) == 2
    assert await _active_peers(session) == {NEW}
    assert (await _chat(session, CHAT)).active is False
    assert (await chats.active_chat(session, tenant.id, "vk")).peer_id == NEW

    await _move(session, tenant_cfg)  # повторный старт с тем же конфигом — без изменений
    assert (await session.scalar(select(func.count()).select_from(Chat))) == 2
    assert await _active_peers(session) == {NEW}

    await _move(session, tenant_cfg, CHAT)  # вернулись в прежнюю беседу — она снова активна
    assert (await session.scalar(select(func.count()).select_from(Chat))) == 2
    assert await _active_peers(session) == {CHAT}
    assert (await chats.active_chat(session, tenant.id, "vk")).peer_id == CHAT


async def test_bootstrap_without_peer_keeps_chats(session, tenant_cfg):
    await apply_tenant_config(session, tenant_cfg)
    await _move(session, tenant_cfg)
    await apply_tenant_config(session, tenant_cfg.model_copy(update={"vk_chat_peer_id": None}))
    assert await _active_peers(session) == {NEW}


async def test_active_chat_filters(session, tenant_cfg):
    tenant = await apply_tenant_config(session, tenant_cfg)
    other = Tenant(key="other_team", name="Другая команда")
    session.add(other)
    await session.flush()
    session.add(Chat(tenant_id=other.id, channel="vk", peer_id="2000000050"))
    await session.commit()

    assert (await chats.active_chat(session, tenant.id, "vk")).peer_id == CHAT
    assert (await chats.active_chat(session, other.id, "vk")).peer_id == "2000000050"
    assert await chats.active_chat(session, tenant.id, "telegram") is None

    # две активные (нештатно, например ручная правка БД) — берётся последняя добавленная
    session.add(Chat(tenant_id=tenant.id, channel="vk", peer_id="2000000051"))
    await session.commit()
    assert (await chats.active_chat(session, tenant.id, "vk")).peer_id == "2000000051"

    for c in (await session.scalars(select(Chat).filter_by(tenant_id=tenant.id))).all():
        c.active = False
    await session.commit()
    assert await chats.active_chat(session, tenant.id, "vk") is None
    assert (await chats.active_chat(session, other.id, "vk")).peer_id == "2000000050", "чужая не задета"


# ---------- polls.relocate ----------


async def _poll_with_vote(session, tenant_cfg, ch: FakeChannel):
    """_ready + матч АБЛ через 2 дня, анонс в тестовой беседе, Петров отметился «Буду»."""
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "g1", days=2)
    await polls.publish(session, ch, await _chat(session, CHAT), m)
    await handle(session, ch, _press(CHAT, PETROV, {"m": m.id, "v": "going"}))
    return tenant, entries, m


async def test_relocate_moves_poll_with_votes_and_closes_old(session, tenant_cfg):
    ch = FakeChannel()
    tenant, _, m = await _poll_with_vote(session, tenant_cfg, ch)
    [old] = await _current(session)
    assert "Буду (1)" in ch.messages[(CHAT, old.message_ref)]
    await _move(session, tenant_cfg)

    assert await polls.relocate(session, ch, tenant.id) == 1
    [text] = _to(ch, NEW)
    assert "✅ Буду (1):\nПетров\n" in text, "отметки привязаны к матчу и переезжают вместе с анонсом"
    [new] = await _current(session)
    new_chat = await _chat(session, NEW)
    assert new.chat_id == new_chat.id and new.match_id == m.id
    await session.refresh(old)
    assert old.superseded_by_id == new.id
    assert ch.pins[-1] == MessageRef(NEW, new.message_ref)
    assert ch.buttons[(NEW, new.message_ref)] == [*polls.vote_buttons(m.id), [polls.BOT_BUTTON]]
    # старый анонс — без кнопок, с пояснением
    assert ch.messages[(CHAT, old.message_ref)] == polls.MOVED_TEXT
    assert ch.buttons[(CHAT, old.message_ref)] is None
    assert len((await session.scalars(select(Attendance))).all()) == 1

    # идемпотентность: повторный перенос ничего не шлёт, дубля нет
    n_sent, n_edits = len(ch.sent), len(ch.edits)
    assert await polls.relocate(session, ch, tenant.id) == 0
    assert len(ch.sent) == n_sent and len(ch.edits) == n_edits
    assert len(await _current(session)) == 1

    # голос в новой беседе обновляет новый анонс
    await handle(session, ch, _press(NEW, SIDOROV, {"m": m.id, "v": "going"}, new.message_ref))
    assert "Буду (2)" in ch.messages[(NEW, new.message_ref)]


async def test_relocate_without_move_or_active_chat(session, tenant_cfg):
    ch = FakeChannel()
    tenant, _, _ = await _poll_with_vote(session, tenant_cfg, ch)
    n = len(ch.sent)
    assert await polls.relocate(session, ch, tenant.id) == 0, "анонс уже в активной беседе"
    for c in (await session.scalars(select(Chat))).all():
        c.active = False
    await session.commit()
    assert await polls.relocate(session, ch, tenant.id) == 0
    assert len(ch.sent) == n and len(await _current(session)) == 1


class FlakyChannel(FakeChannel):
    """Отправка в новую беседу падает, пока fail = True; считает попытки."""

    def __init__(self):
        super().__init__()
        self.fail = False
        self.attempts = 0

    async def send(self, peer_id, text, buttons=None):
        if peer_id == NEW:
            self.attempts += 1
            if self.fail:
                raise ChannelError(917, "нет доступа к беседе")
        return await super().send(peer_id, text, buttons)


async def test_relocate_channel_error_retries_once_per_hour(session, tenant_cfg, monkeypatch):
    ch = FlakyChannel()
    tenant, _, m = await _poll_with_vote(session, tenant_cfg, ch)
    await _move(session, tenant_cfg)
    clock = [utcnow().replace(minute=5)]
    monkeypatch.setattr(polls, "utcnow", lambda: clock[0])

    ch.fail = True
    assert await polls.relocate(session, ch, tenant.id) == 0
    assert ch.attempts == 1
    [old] = await _current(session)
    assert (await session.get(Chat, old.chat_id)).peer_id == CHAT, "анонс остался в прежней беседе"
    assert ch.messages[(CHAT, old.message_ref)] != polls.MOVED_TEXT, "старый не закрыт"

    clock[0] += timedelta(minutes=50)  # тот же час — не пытаемся
    assert await polls.relocate(session, ch, tenant.id) == 0
    assert ch.attempts == 1

    clock[0] += timedelta(minutes=10)  # следующий час — новая попытка, VK ожил
    ch.fail = False
    assert await polls.relocate(session, ch, tenant.id) == 1
    assert ch.attempts == 2
    [new] = await _current(session)
    assert (await session.get(Chat, new.chat_id)).peer_id == NEW
    assert len(_to(ch, NEW)) == 1


async def test_relocate_skips_started_and_finished(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    abl = entries["abl"]
    ch = FakeChannel()
    started = await _rel_match(session, abl, "started", days=-0.01)
    played = await _rel_match(session, abl, "played", days=1)
    cancelled = await _rel_match(session, abl, "cancelled", days=1)
    no_time = await _rel_match(session, abl, "no-time", days=3, with_time=False)
    played.status, cancelled.status = "played", "cancelled"
    await session.commit()
    chat = await _chat(session, CHAT)
    for m in (started, played, cancelled, no_time):
        await polls.publish(session, ch, chat, m)
    await _move(session, tenant_cfg)

    assert await polls.relocate(session, ch, tenant.id) == 1, "переезжает только матч без времени"
    new_chat = await _chat(session, NEW)
    by_match = {p.match_id: p.chat_id for p in await _current(session)}
    assert by_match[no_time.id] == new_chat.id
    assert {by_match[m.id] for m in (started, played, cancelled)} == {chat.id}
    assert len(_to(ch, NEW)) == 1


# ---------- weekly.tick после смены беседы ----------


async def test_tick_after_move_one_poll_in_new_chat(session, tenant_cfg, monkeypatch):
    from tests.test_weekly import WED, _week_match, at

    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")  # сб 12:00, опрос в среду 10:00
    ch = FakeChannel()
    clock = [None]
    monkeypatch.setattr(polls, "utcnow", lambda: clock[0])

    async def tick(now):
        clock[0] = naive_utc(now)
        await weekly.tick(session, ch, now=now)

    await tick(at(WED, 10))
    assert len(_to(ch, CHAT)) == 1
    await _move(session, tenant_cfg)
    for hh in (11, 12, 13):
        await tick(at(WED, hh))
    [poll] = await _current(session)
    assert poll.match_id == m.id and poll.chat_id == (await _chat(session, NEW)).id
    assert len(_to(ch, NEW)) == 1, "в новой беседе один анонс, без дубля от _ensure_poll"
    assert ch.messages[(CHAT, "101")] == polls.MOVED_TEXT
    assert len(_to(ch, CHAT)) == 1, "в прежнюю беседу ничего нового не отправлено"


async def test_tick_after_move_before_poll_publishes_in_new_chat(session, tenant_cfg, monkeypatch):
    from tests.test_weekly import WED, _week_match, at

    tenant, entries = await _ready(session, tenant_cfg)
    await _week_match(session, entries["abl"], "g1")
    await _move(session, tenant_cfg)
    ch = FakeChannel()
    monkeypatch.setattr(polls, "utcnow", lambda: naive_utc(at(WED, 10)))
    await weekly.tick(session, ch, now=at(WED, 10))
    assert _to(ch, CHAT) == [] and len(_to(ch, NEW)) == 1


# ---------- рассылки — в активную беседу ----------


async def test_remind_in_active_chat(session, tenant_cfg):
    tenant, entries, m = await _abl_poll(session, tenant_cfg)
    await _close_dm(session, SIDOROV)
    await _move(session, tenant_cfg)
    ch = FakeChannel()
    ch.members = [SIDOROV]
    await weekly._remind_silent(session, ch, entries["abl"], m, _after_poll())
    [(peer, text)] = ch.mention_sent
    assert peer == NEW and "Сидоров" in text


async def test_change_message_in_active_chat(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "g1", days=3)
    await _move(session, tenant_cfg)
    session.add(ChangeEvent(kind=ChangeKind.TIME_SET, match_id=m.id))
    await session.commit()
    ch = FakeChannel()
    await scheduler.process_changes(session, ch)
    assert _to(ch, CHAT) == []
    assert len(_to(ch, NEW)) == 1 and _to(ch, NEW)[0].startswith("⏰ Назначено время матча")


async def test_leader_announce_in_active_chat(session, tenant_cfg):
    tenant = await apply_tenant_config(session, tenant_cfg)
    await _move(session, tenant_cfg)
    ch = FakeChannel()
    await leader._announce_in_chat(session, ch, tenant.id, "объявление")
    assert ch.sent == [(NEW, "объявление", None)]


async def test_post_poll_cli_uses_active_chat(session, tenant_cfg, monkeypatch, tmp_path):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "g1", days=2)
    await _move(session, tenant_cfg)
    await session.commit()
    ch = FakeChannel()

    @contextlib.asynccontextmanager
    async def client():
        yield None

    monkeypatch.setattr(app, "make_client", client)
    monkeypatch.setattr(app, "make_vk_channel", lambda settings, c: ch)
    settings = Settings(_env_file=None, db_path=tmp_path / "test.db")
    result = await app.post_poll(settings, m.id)
    assert NEW in result
    assert len(_to(ch, NEW)) == 1 and _to(ch, CHAT) == []


# ---------- обработчики в прежней беседе ----------


async def test_old_chat_buttons_get_toast_and_vote_ignored(session, tenant_cfg):
    ch = FakeChannel()
    tenant, _, m = await _poll_with_vote(session, tenant_cfg, ch)
    await _move(session, tenant_cfg)
    before = await _row_counts(session)
    n_sent, n_edits = len(ch.sent), len(ch.edits)
    toasts = len(ch.toasts)
    for payload in ({"m": m.id, "v": "not_going"}, {"m": m.id, "v": "going"}, {"c": "игры"}, {"a": "gm"}):
        await handle(session, ch, _press(CHAT, SIDOROV, payload))
    assert ch.toasts[toasts:] == [MOVED_CHAT_TOAST] * 4
    assert len(ch.sent) == n_sent and len(ch.edits) == n_edits
    assert await _row_counts(session) == before, "голос в прежней беседе не учитывается"
    petrov = await _player(session, tenant, PETROV)
    row = (await session.execute(select(Attendance).filter_by(player_id=petrov.id))).scalar_one()
    assert row.vote == "going"


async def test_old_chat_mention_gets_moved_text(session, tenant_cfg):
    await _ready(session, tenant_cfg)
    await _move(session, tenant_cfg)
    before = await _row_counts(session)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(CHAT, PETROV, "игры", is_chat=True, mentioned=True))
    assert ch.sent == [(CHAT, MOVED_CHAT, None)]
    await handle(session, ch, IncomingMessage(CHAT, PETROV, "игры", is_chat=True, mentioned=False))
    assert len(ch.sent) == 1, "без обращения — молчим"
    await handle(session, ch, IncomingMessage(UNKNOWN_PEER, PETROV, "игры", is_chat=True, mentioned=True))
    assert ch.sent[-1] == (UNKNOWN_PEER, UNKNOWN_CHAT, None), "незнакомая беседа — прежний ответ"
    assert await _row_counts(session) == before

    # «игры» в беседу — руководителю (ADR 0008); игроку ответ ушёл бы в личку
    await handle(session, ch, IncomingMessage(NEW, CAPTAIN, "игры", is_chat=True, mentioned=True))
    assert ch.sent[-1][0] == NEW and ch.sent[-1][1] not in (MOVED_CHAT, UNKNOWN_CHAT), "новая беседа работает"


async def test_old_chat_invite_creates_no_player(session, tenant_cfg):
    await _ready(session, tenant_cfg)
    await _move(session, tenant_cfg)
    before = await _row_counts(session)
    ch = FakeChannel(names={"50": "Олег Новиков"})
    invite = IncomingMessage(
        CHAT, CAPTAIN, "", is_chat=True, mentioned=False, action="chat_invite_user", action_member_id="50"
    )
    await handle(session, ch, invite)
    assert ch.sent == [] and await _row_counts(session) == before
    assert (await session.scalars(select(MessengerAccount).filter_by(external_user_id="50"))).all() == []


async def test_dm_vote_still_works_after_move(session, tenant_cfg):
    ch = FakeChannel()
    tenant, _, m = await _poll_with_vote(session, tenant_cfg, ch)
    await _move(session, tenant_cfg)
    await handle(session, ch, _press(SIDOROV, SIDOROV, {"m": m.id, "v": "going"}, "777"))
    assert ch.toasts[-1] == "Отмечено: Буду"
    sidorov = await _player(session, tenant, SIDOROV)
    assert (await session.execute(select(Attendance).filter_by(player_id=sidorov.id))).scalar_one()
