"""M3: интерфейс руководителя в личке через handle() — передача прав, «исправить», новые участники,
«Это ошибка», ограничения клавиатуры VK (вымышленные имена)."""

import pytest
from sqlalchemy import select

from basket_assist.bot import leader
from basket_assist.bot.handlers import handle
from basket_assist.channels.base import IncomingMessage
from basket_assist.services import leaders, roster
from basket_assist.storage.tables import (
    MessengerAccount,
    NotificationLog,
    PermissionGrant,
    Player,
    SeasonRosterEntry,
)
from tests.fakes import FakeChannel
from tests.test_roster import CAPTAIN, CHAT, MEMBERS, VICE, _entry_id, _player, _press, _setup

PETROV, SIDOROV, FRIEND = "10", "11", "13"


async def _open_dm(session, *vk_ids: str) -> None:
    for vk in vk_ids:
        acc = (await session.execute(select(MessengerAccount).filter_by(external_user_id=vk))).scalar_one()
        acc.dm_allowed = True
    await session.commit()


async def _ready(session, tenant_cfg):
    """Беседа импортирована, однозначные подтверждены капитаном, личка открыта у всех."""
    tenant, entries = await _setup(session, tenant_cfg)
    await roster.import_chat_members(session, tenant.id, MEMBERS, [], None)
    captain = await _player(session, tenant, CAPTAIN)
    await roster.confirm_all_unambiguous(session, tenant.id, captain.id)
    await _open_dm(session, CAPTAIN, VICE, PETROV, SIDOROV, FRIEND)
    return tenant, entries


def _dms(ch: FakeChannel, peer: str) -> list[str]:
    return [t for p, t, _ in ch.sent if p == peer]


def _assert_vk_keyboard(rows) -> None:
    rows = rows or []
    buttons = [b for row in rows for b in row]
    assert len(rows) <= 6, f"рядов {len(rows)}"
    assert len(buttons) <= 10, f"кнопок {len(buttons)}"
    for b in buttons:
        assert 0 < len(b.label) <= 40, b.label


# ---------- Передача прав через кнопки ----------


async def test_grant_flow_g1_g2_g3_and_revoke(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    ch = FakeChannel()
    petrov = await _player(session, tenant, PETROV)
    captain = await _player(session, tenant, CAPTAIN)

    await handle(session, ch, IncomingMessage(CAPTAIN, CAPTAIN, "права", is_chat=False, mentioned=False))
    menu = ch.sent[-1][2]
    assert {b.payload["p"] for row in menu for b in row if b.payload["a"] == "g1"} >= {petrov.id}

    await handle(session, ch, _press(CAPTAIN, {"a": "g1", "p": petrov.id}))
    rows = ch.buttons[(CAPTAIN, "500")]
    assert sorted(b.payload["d"] for row in rows for b in row) == [0, 3, 7, 14]
    await handle(session, ch, _press(CAPTAIN, {"a": "g2", "p": petrov.id, "d": 7}))
    rows = ch.buttons[(CAPTAIN, "500")]
    assert {b.payload["s"] for row in rows for b in row} == {0, 1}
    await handle(session, ch, _press(CAPTAIN, {"a": "g3", "p": petrov.id, "d": 7, "s": 1}))
    assert ch.toasts[-1] == "Права переданы"

    grant = (await session.execute(select(PermissionGrant))).scalar_one()
    assert grant.player_id == petrov.id and grant.granted_by_player_id == captain.id
    assert grant.valid_until is not None and grant.suppress_digests_for_grantor
    assert await leaders.is_leader(session, tenant.id, petrov.id)
    assert captain.id not in {p.id for p in await leaders.digest_recipients(session, tenant.id)}
    assert any("переданы права капитана" in t for t in _dms(ch, PETROV))
    chat_msgs = _dms(ch, CHAT)
    assert len(chat_msgs) == 1 and "обязанности капитана исполняет Саша Петров" in chat_msgs[0]
    # в меню появилась кнопка «Отозвать»
    revoke = [b for row in ch.buttons[(CAPTAIN, "500")] for b in row if b.payload["a"] == "gr"]
    assert [b.payload["g"] for b in revoke] == [grant.id]

    # и. о. не может ни передать права дальше, ни отозвать
    for payload in (
        {"a": "gm"},
        {"a": "g1", "p": captain.id},
        {"a": "g3", "p": captain.id, "d": 0, "s": 0},
        {"a": "gr", "g": grant.id},
    ):
        await handle(session, ch, _press(PETROV, payload))
        assert ch.toasts[-1] == "Передавать права может только капитан или заместитель"
    assert len((await session.scalars(select(PermissionGrant))).all()) == 1
    await session.refresh(grant)
    assert grant.revoked_at is None

    # заместитель может отозвать передачу, сделанную капитаном
    await handle(session, ch, _press(VICE, {"a": "gr", "g": grant.id}))
    assert ch.toasts[-1] == "Права отозваны"
    assert not await leaders.is_leader(session, tenant.id, petrov.id)
    assert _dms(ch, PETROV)[-1] == "Передача прав капитана отменена."


async def test_grant_denied_for_non_leader_and_unmatched(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    ch = FakeChannel()
    friend = await _player(session, tenant, FRIEND)  # «Друг Команды» — не сопоставлен
    petrov = await _player(session, tenant, PETROV)

    # обычный игрок — даже не руководитель
    await handle(session, ch, _press(SIDOROV, {"a": "g3", "p": petrov.id, "d": 0, "s": 0}))
    assert ch.toasts[-1] == "Это доступно только руководителям команды"
    await handle(session, ch, IncomingMessage(SIDOROV, SIDOROV, "права", is_chat=False, mentioned=False))
    assert "Кому передать" not in ch.sent[-1][1]

    # несопоставленному передать нельзя — ни в меню, ни подделанной кнопкой
    text, rows = await leader.rights_menu(session, tenant.id)
    assert friend.id not in {b.payload.get("p") for row in rows for b in row}
    await handle(session, ch, _press(CAPTAIN, {"a": "g3", "p": friend.id, "d": 3, "s": 0}))
    assert ch.toasts[-1] == "права можно передать только сопоставленному игроку"
    assert (await session.scalars(select(PermissionGrant))).all() == []
    assert _dms(ch, CHAT) == []
    assert not await leaders.is_leader(session, tenant.id, friend.id)


async def test_rights_menu_with_grants_keeps_paging(session, tenant_cfg):
    """С активными передачами прав меню не должно терять игроков и кнопку «Далее →»."""
    tenant, entries = await _ready(session, tenant_cfg)
    captain = await _player(session, tenant, CAPTAIN)
    entry = entries["abl"]
    matched = []
    for i in range(6):
        p = Player(tenant_id=tenant.id, display_name=f"Игрок{i} Тестовый")
        r = SeasonRosterEntry(
            league_team_id=entry.league_team_id,
            tournament_id=entry.tournament_id,
            external_person_id=f"x{i}",
            last_name="Тестовый",
            first_name=f"Игрок{i}",
        )
        session.add_all([p, r])
        await session.flush()
        await roster.confirm(session, tenant.id, p.id, entry.id, r.id, captain.id)
        matched.append(p)
    for p in matched[:2]:
        await leaders.grant_acting(session, tenant.id, captain.id, p.id, None)
    await session.commit()

    all_matched = set((await session.scalars(select(Player.id).where(Player.active.is_(True)))).all())
    seen, page = set(), 0
    while True:
        _, rows = await leader.rights_menu(session, tenant.id, page)
        _assert_vk_keyboard(rows)
        payloads = [b.payload for row in rows for b in row]
        seen |= {pl["p"] for pl in payloads if pl["a"] == "g1"}
        nxt = [pl for pl in payloads if pl["a"] == "gm"]
        if not nxt:
            break
        page = nxt[0]["pg"]
    matched_ids = {p.id for p in matched[2:]}  # действующих и. о. (первые двое) повторно не предлагаем
    assert matched_ids <= seen, "часть сопоставленных игроков недоступна в меню «права»"
    assert not ({p.id for p in matched[:2]} & seen), "действующие и. о. не должны предлагаться повторно"
    assert seen <= all_matched


# ---------- «исправить» и «Вернуть в команду» ----------


async def test_fix_not_team_player_and_back(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    ch = FakeChannel()
    friend = await _player(session, tenant, FRIEND)

    await handle(session, ch, _press(VICE, {"a": "lx", "p": friend.id, "e": entries["abl"].id}))
    assert ch.toasts[-1] == "Отмечено: не игрок команды"
    assert friend.id not in {i.player.id for i in await roster.queue(session, tenant.id)}

    await handle(session, ch, IncomingMessage(VICE, VICE, "исправить друг", is_chat=False, mentioned=False))
    rows = ch.sent[-1][2]
    assert [b.label for row in rows for b in row] == ["Друг Команды (не игрок)"]
    await handle(session, ch, _press(VICE, {"a": "lfix", "p": friend.id}))
    assert "не игрок команды" in ch.messages[(VICE, "500")]
    back = [b for row in ch.buttons[(VICE, "500")] for b in row if b.payload["a"] == "lback"]
    assert len(back) == 1

    await handle(session, ch, _press(VICE, back[0].payload))
    assert ch.toasts[-1] == "Возвращён в команду"
    await session.refresh(friend)
    assert friend.active
    assert "не игрок команды" not in ch.messages[(VICE, "500")]
    assert not [b for row in ch.buttons[(VICE, "500")] for b in row if b.payload["a"] == "lback"]
    assert friend.id in {i.player.id for i in await roster.queue(session, tenant.id)}

    # «Вернуть в команду» — только для руководителей
    await handle(session, ch, _press(FRIEND, {"a": "lx", "p": friend.id, "e": entries["abl"].id}))
    assert ch.toasts[-1] == "Это доступно только руководителям команды"
    await session.refresh(friend)
    assert friend.active


async def test_fix_change_confirmed_link(session, tenant_cfg):
    """«исправить» → «Изменить: лига» → «Нет в заявке» меняет уже подтверждённую связь."""
    tenant, entries = await _ready(session, tenant_cfg)
    ch = FakeChannel()
    petrov = await _player(session, tenant, PETROV)
    abl = entries["abl"]
    await handle(session, ch, _press(CAPTAIN, {"a": "le", "p": petrov.id, "e": abl.id}))
    assert "Сейчас: сопоставлен: Петров Александр №7" in ch.messages[(CAPTAIN, "500")]
    await handle(session, ch, _press(CAPTAIN, {"a": "ln", "p": petrov.id, "e": abl.id}))
    assert await roster.roster_status(session, tenant.id, petrov.id, abl.id) == "not_in_roster"
    assert any("Вас нет в заявке на сезон" in t for t in _dms(ch, PETROV))


async def test_dm_leader_commands_hidden_from_players(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    ch = FakeChannel()
    for cmd in ("сопоставление", "исправить петров", "права"):
        await handle(session, ch, IncomingMessage(SIDOROV, SIDOROV, cmd, is_chat=False, mentioned=False))
        assert ch.sent[-1][2] is None and "Для руководителей" not in ch.sent[-1][1]
    await handle(session, ch, IncomingMessage(CAPTAIN, CAPTAIN, "исправить", is_chat=False, mentioned=False))
    assert "исправить <имя" in ch.sent[-1][1]


# ---------- Новый участник беседы и «Это ошибка» ----------


def _invite(member: str) -> IncomingMessage:
    return IncomingMessage(
        CHAT, CAPTAIN, "", is_chat=True, mentioned=False, action="chat_invite_user", action_member_id=member
    )


async def test_new_member_creates_player_and_notifies_once(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    ch = FakeChannel(names={"50": "Олег Новиков"})

    await handle(session, ch, _invite("50"))
    await handle(session, ch, _invite("50"))  # повторное событие — без дублей
    await handle(session, ch, _invite("-777"))  # сообщество, не человек

    accs = (await session.scalars(select(MessengerAccount).filter_by(external_user_id="50"))).all()
    assert len(accs) == 1
    novikov = await session.get(Player, accs[0].player_id)
    assert novikov.display_name == "Олег Новиков" and novikov.tenant_id == tenant.id
    assert (await session.scalars(select(MessengerAccount).filter_by(external_user_id="-777"))).all() == []
    for lead in (CAPTAIN, VICE):
        msgs = [t for t in _dms(ch, lead) if "Олег Новиков" in t]
        assert len(msgs) == 1 and "сопоставление" in msgs[0]
    assert _dms(ch, CHAT) == []  # в беседу бот ничего не пишет
    assert novikov.id in {i.player.id for i in await roster.queue(session, tenant.id)}
    logs = (await session.scalars(select(NotificationLog).filter_by(kind="new_member"))).all()
    assert len(logs) == 1


async def test_error_button_notifies_leaders(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(SIDOROV, SIDOROV, "мои данные", is_chat=False, mentioned=False))
    err = ch.sent[-1][2][0][0]
    assert err.payload == {"a": "err"} and "Сидоров Евгений" in ch.sent[-1][1]

    await handle(session, ch, _press(SIDOROV, err.payload))
    assert ch.toasts[-1] == "Сообщил руководителям команды"
    for lead in (CAPTAIN, VICE):
        msgs = [t for t in _dms(ch, lead) if "сообщает об ошибке" in t]
        assert len(msgs) == 1 and "Evgeniy Sidorov" in msgs[0]
    assert ch.edits == []  # карточку «Мои данные» не заменяем


# ---------- Ограничения клавиатуры VK и «Выбрать из заявки» ----------


async def _big_roster(session, entry, n: int) -> None:
    for i in range(n):
        session.add(
            SeasonRosterEntry(
                league_team_id=entry.league_team_id,
                tournament_id=entry.tournament_id,
                external_person_id=f"big{i}",
                last_name=f"Оченьдлиннаясоставнаяфамилия-Тестовая{i:02d}",
                first_name="Константин",
                number=str(i),
            )
        )
    await session.commit()


async def test_all_leader_cards_fit_vk_keyboard(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    await _big_roster(session, entries["abl"], 20)
    members = MEMBERS + [
        {"vk_id": 20 + i, "first_name": "Константин", "last_name": "Тестовый" + "о" * 40} for i in range(8)
    ]
    await roster.import_chat_members(session, tenant.id, members, [], None)
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, PETROV)
    abl = entries["abl"]

    _assert_vk_keyboard((await leader.matching_summary(session, tenant.id))[1])
    _assert_vk_keyboard((await leader.matching_card(session, tenant.id))[1])
    for item in await roster.queue(session, tenant.id):
        _assert_vk_keyboard((await leader._card(session, tenant.id, item))[1])
    _assert_vk_keyboard((await leader.item_card(session, tenant.id, petrov.id, abl.id))[1])
    _assert_vk_keyboard((await leader.find_players(session, tenant.id, "о"))[1])
    _assert_vk_keyboard((await leader.player_overview(session, tenant.id, petrov.id))[1])
    for page in range(5):
        _assert_vk_keyboard((await leader.roster_page(session, tenant.id, petrov.id, abl.id, page))[1])

    await roster.confirm_all_unambiguous(session, tenant.id, captain.id)
    for p in (await session.scalars(select(Player).where(Player.display_name.like("Константин%")))).all():
        r = (await leader.roster_page(session, tenant.id, p.id, abl.id, 0))[1][0][0].payload["r"]
        await roster.confirm(session, tenant.id, p.id, abl.id, r, captain.id)
        await leaders.grant_acting(session, tenant.id, captain.id, p.id, None)
    await roster.mark_not_team_player(session, tenant.id, petrov.id)
    await session.commit()
    _assert_vk_keyboard((await leader.player_overview(session, tenant.id, petrov.id))[1])
    for page in range(3):
        _assert_vk_keyboard((await leader.rights_menu(session, tenant.id, page))[1])

    # g1 / g2 — через handle
    ch = FakeChannel()
    for payload in ({"a": "g1", "p": petrov.id}, {"a": "g2", "p": petrov.id, "d": 3}):
        await handle(session, ch, _press(CAPTAIN, payload))
        _assert_vk_keyboard(ch.buttons[(CAPTAIN, "500")])


async def test_roster_page_hides_taken_rows(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    abl = entries["abl"]
    await _big_roster(session, abl, 10)  # 13 строк в АБЛ → 3 страницы
    await roster.import_chat_members(session, tenant.id, MEMBERS, [], None)
    vice = await _player(session, tenant, VICE)
    petrov = await _player(session, tenant, PETROV)
    sidorov = await _player(session, tenant, SIDOROV)
    a1 = await _entry_id(session, abl, "a1")
    big0 = await _entry_id(session, abl, "big0")
    await roster.confirm(session, tenant.id, petrov.id, abl.id, a1, vice.id)
    await roster.confirm(session, tenant.id, vice.id, abl.id, big0, vice.id)
    await session.commit()

    shown, page, pages_text = [], 0, ""
    while True:
        text, rows = await leader.roster_page(session, tenant.id, sidorov.id, abl.id, page)
        pages_text = pages_text or text
        payloads = [b.payload for row in rows for b in row]
        shown += [pl["r"] for pl in payloads if pl["a"] == "lk"]
        nxt = [pl for pl in payloads if pl.get("pg") == page + 1]
        if not nxt:
            break
        page += 1
    assert a1 not in shown and big0 not in shown
    assert len(shown) == len(set(shown)) == 11  # 13 − 2 занятых
    assert "стр. 1/2" in pages_text

    # подделанная кнопка с занятой строкой — конфликт, связь не меняется
    ch = FakeChannel()
    await handle(session, ch, _press(VICE, {"a": "lk", "p": sidorov.id, "e": abl.id, "r": a1}))
    assert ch.toasts[-1] == "Эта строка уже связана с другим игроком"
    assert await roster.roster_status(session, tenant.id, sidorov.id, abl.id) == "unmatched"


@pytest.mark.parametrize("cmd", ["сопоставление", "права"])
async def test_leader_dm_menus_fit_vk_keyboard(session, tenant_cfg, cmd):
    await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(CAPTAIN, CAPTAIN, cmd, is_chat=False, mentioned=False))
    _assert_vk_keyboard(ch.sent[-1][2])
