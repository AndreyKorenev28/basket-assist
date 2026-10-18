"""M3: интерфейс руководителя в личке через handle() — передача прав, «исправить», новые участники,
«Это ошибка», ограничения клавиатуры VK (вымышленные имена)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from basket_assist.bot import leader
from basket_assist.bot.handlers import handle
from basket_assist.channels.base import ButtonPress, IncomingMessage, MessageRef
from basket_assist.domain.matching import Candidate, Confidence
from basket_assist.domain.models import RosterLinkStatus
from basket_assist.services import leaders, polls, roster
from basket_assist.storage.tables import (
    Chat,
    League,
    LeagueTeam,
    Match,
    MessengerAccount,
    NotificationLog,
    PermissionGrant,
    Player,
    RosterLink,
    SeasonRosterEntry,
    TeamEntry,
    Tenant,
    Tournament,
    utcnow,
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
    # игроку при отметке руководителем не пишем — только при «Буду» (polls); остальным руководителям — уведомление
    assert not _dms(ch, PETROV)
    vice_dms = [t for t in _dms(ch, VICE) if "нет в заявке на сезон" in t]
    assert len(vice_dms) == 1
    captain = await _player(session, tenant, CAPTAIN)
    assert vice_dms[0].startswith(f"Отметка от {captain.display_name}:")
    assert petrov.display_name in vice_dms[0] and "Нужно дозаявить" in vice_dms[0]
    assert not [t for t in _dms(ch, CAPTAIN) if "Отметка от" in t]


async def test_going_not_in_roster_warns_player_and_leaders(session, tenant_cfg):
    """«Буду» от игрока со статусом «нет в заявке на сезон» — предупреждение игроку и руководителям, один раз."""
    tenant, entries = await _ready(session, tenant_cfg)
    entry = entries["abl"]
    petrov = await _player(session, tenant, PETROV)
    captain = await _player(session, tenant, CAPTAIN)
    await roster.mark_not_in_roster(session, tenant.id, petrov.id, entry.id, captain.id)
    rival = LeagueTeam(
        league_id=(await session.get(LeagueTeam, entry.league_team_id)).league_id,
        external_id="x",
        name="Соперник",
    )
    session.add(rival)
    await session.flush()
    start = utcnow() + timedelta(days=2)
    m = Match(
        tournament_id=entry.tournament_id,
        external_id="g1",
        home_team_id=entry.league_team_id,
        away_team_id=rival.id,
        match_date=start.date(),
        start_at=start,
        time_status="time_set",
        status="scheduled",
    )
    session.add(m)
    await session.commit()
    ch = FakeChannel()
    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, ch, chat, m)
    ref = MessageRef(CHAT, "101")

    await handle(session, ch, ButtonPress(CHAT, PETROV, ref, {"m": m.id, "v": "going"}, "e"))
    mine = [t for t in _dms(ch, PETROV) if "Буду" in t]
    assert len(mine) == 1 and "нет в заявке на сезон" in mine[0]
    for lead in (CAPTAIN, VICE):
        alerts = [t for t in _dms(ch, lead) if "нет в заявке на сезон" in t]
        assert len(alerts) == 1 and petrov.display_name in alerts[0]

    # «Не буду» → снова «Буду»: сигнал не повторяется
    await handle(session, ch, ButtonPress(CHAT, PETROV, ref, {"m": m.id, "v": "not_going"}, "e"))
    await handle(session, ch, ButtonPress(CHAT, PETROV, ref, {"m": m.id, "v": "going"}, "e"))
    assert len([t for t in _dms(ch, PETROV) if "нет в заявке на сезон" in t]) == 1


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


# ---------- Очередь сопоставления «по одному»: дальше по кругу ----------


def _card_item(ch: FakeChannel, user: str = VICE) -> tuple[int, int]:
    """(игрок, выступление) карточки, показанной в сообщении с кнопками — по кнопке «Пропустить»."""
    pl = next(b.payload for row in ch.buttons[(user, "500")] for b in row if b.payload["a"] == "lskip")
    return pl["p"], pl["e"]


async def _queue_keys(session, tenant) -> list[tuple[int, int]]:
    return [(i.player.id, i.entry.id) for i in await roster.queue(session, tenant.id)]


async def _imported(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    await roster.import_chat_members(session, tenant.id, MEMBERS, [], None)
    return tenant, entries


async def test_queue_skip_then_confirm_goes_forward_not_back(session, tenant_cfg):
    tenant, _ = await _imported(session, tenant_cfg)
    keys = await _queue_keys(session, tenant)
    assert len(keys) >= 4
    a, b, c = keys[0], keys[1], keys[2]
    ch = FakeChannel()

    await handle(session, ch, _press(VICE, {"a": "lnext"}))
    assert _card_item(ch) == a
    assert f"Осталось в очереди: {len(keys)}" in ch.messages[(VICE, "500")]

    await handle(session, ch, _press(VICE, {"a": "lskip", "p": a[0], "e": a[1]}))
    assert ch.toasts[-1] == "Пропущено"
    assert _card_item(ch) == b

    # подтвердить B кандидатом из карточки → следующая C, а не пропущенная A
    lk = next(bt.payload for row in ch.buttons[(VICE, "500")] for bt in row if bt.payload["a"] == "lk")
    await handle(session, ch, _press(VICE, lk))
    assert ch.toasts[-1] == "Связь подтверждена"
    assert _card_item(ch) == c
    assert f"Осталось в очереди: {len(keys) - 1}" in ch.messages[(VICE, "500")]  # полный размер очереди

    # «Нет в заявке» и «Не игрок команды» тоже ведут дальше
    await handle(session, ch, _press(VICE, {"a": "ln", "p": c[0], "e": c[1]}))
    after_c = keys[3]
    assert _card_item(ch) == after_c

    # пропущенная A возвращается только после последнего элемента очереди
    seen = []
    cur = after_c
    while cur != a:
        seen.append(cur)
        await handle(session, ch, _press(VICE, {"a": "lskip", "p": cur[0], "e": cur[1]}))
        cur = _card_item(ch)
        assert len(seen) <= len(keys), "очередь не вернулась к началу"
    rest = await _queue_keys(session, tenant)
    assert seen == rest[rest.index(after_c) :]  # прошли весь хвост очереди по порядку
    assert seen[-1] == rest[-1]


async def test_queue_not_team_player_goes_forward(session, tenant_cfg):
    tenant, _ = await _imported(session, tenant_cfg)
    keys = await _queue_keys(session, tenant)
    a = keys[0]
    ch = FakeChannel()
    await handle(session, ch, _press(VICE, {"a": "lskip", "p": a[0], "e": a[1]}))
    b = _card_item(ch)
    await handle(session, ch, _press(VICE, {"a": "lx", "p": b[0], "e": b[1]}))
    assert ch.toasts[-1] == "Отмечено: не игрок команды"
    # игрок B уходит из очереди целиком (во всех лигах); дальше — следующий после B, не A
    rest = await _queue_keys(session, tenant)
    assert all(p != b[0] for p, _ in rest)
    assert _card_item(ch) == keys[2]


async def test_queue_cancel_returns_same_card(session, tenant_cfg):
    tenant, _ = await _imported(session, tenant_cfg)
    keys = await _queue_keys(session, tenant)
    b = keys[1]
    ch = FakeChannel()
    await handle(session, ch, _press(VICE, {"a": "lskip", "p": keys[0][0], "e": keys[0][1]}))
    assert _card_item(ch) == b
    await handle(session, ch, _press(VICE, {"a": "ls", "p": b[0], "e": b[1], "pg": 0}))
    assert "Кто это" in ch.messages[(VICE, "500")]
    cancel = next(bt.payload for row in ch.buttons[(VICE, "500")] for bt in row if bt.payload["a"] == "lcard")
    await handle(session, ch, _press(VICE, cancel))
    assert _card_item(ch) == b


async def test_queue_conflict_returns_same_card(session, tenant_cfg):
    tenant, entries = await _imported(session, tenant_cfg)
    abl = entries["abl"]
    vice = await _player(session, tenant, VICE)
    petrov = await _player(session, tenant, PETROV)
    sidorov = await _player(session, tenant, SIDOROV)
    a1 = await _entry_id(session, abl, "a1")
    await roster.confirm(session, tenant.id, petrov.id, abl.id, a1, vice.id)
    await session.commit()
    ch = FakeChannel()
    await handle(session, ch, _press(VICE, {"a": "lk", "p": sidorov.id, "e": abl.id, "r": a1}))
    assert ch.toasts[-1] == "Эта строка уже связана с другим игроком"
    assert _card_item(ch) == (sidorov.id, abl.id)


async def test_queue_wraps_after_last_and_lnext_starts_over(session, tenant_cfg):
    tenant, _ = await _imported(session, tenant_cfg)
    keys = await _queue_keys(session, tenant)
    first, last = keys[0], keys[-1]
    ch = FakeChannel()
    await handle(session, ch, _press(VICE, {"a": "lskip", "p": last[0], "e": last[1]}))
    assert _card_item(ch) == first

    # последний решён (его больше нет в очереди) → тоже по кругу к началу
    await handle(session, ch, _press(VICE, {"a": "ln", "p": last[0], "e": last[1]}))
    assert _card_item(ch) == first

    # «Разобрать по одному» — всегда с начала
    mid = keys[3]
    await handle(session, ch, _press(VICE, {"a": "lskip", "p": mid[0], "e": mid[1]}))
    assert _card_item(ch) != first
    await handle(session, ch, _press(VICE, {"a": "lnext"}))
    assert _card_item(ch) == first


async def test_matching_card_after_unknown_or_same(session, tenant_cfg):
    tenant, _ = await _imported(session, tenant_cfg)
    keys = await _queue_keys(session, tenant)
    text, _ = await leader.matching_card(session, tenant.id, after=(999999, 999999))
    assert f"Осталось в очереди: {len(keys)}" in text  # несуществующий after — с начала, без ошибки
    _, rows = await leader.matching_card(session, tenant.id, after=keys[2], same=True)
    skip = next(b.payload for row in rows for b in row if b.payload["a"] == "lskip")
    assert (skip["p"], skip["e"]) == keys[2]


# ---------- «Не играет в лиге» (not_in_league) ----------


async def test_not_in_league_only_this_league_no_dms_and_goes_forward(session, tenant_cfg):
    tenant, entries = await _imported(session, tenant_cfg)
    await _open_dm(session, CAPTAIN, VICE, PETROV, SIDOROV, FRIEND)
    abl, mlbl = entries["abl"], entries["mlbl"]
    sidorov = await _player(session, tenant, SIDOROV)
    keys = await _queue_keys(session, tenant)
    target = (sidorov.id, mlbl.id)
    assert target in keys and (sidorov.id, abl.id) in keys
    rest = [k for k in keys if k != target]
    pos = keys.index(target)
    expected = keys[pos + 1] if pos + 1 < len(keys) else rest[0]

    ch = FakeChannel()
    await handle(session, ch, _press(VICE, {"a": "lnl", "p": sidorov.id, "e": mlbl.id}))
    assert ch.toasts[-1] == "Отмечено: не играет в этой лиге"
    assert await roster.roster_status(session, tenant.id, sidorov.id, mlbl.id) == "not_in_league"
    assert await roster.roster_status(session, tenant.id, sidorov.id, abl.id) == "unmatched"
    now = await _queue_keys(session, tenant)
    assert target not in now and (sidorov.id, abl.id) in now
    assert now == rest
    assert ch.sent == []  # никаких ЛС — ни игроку, ни руководителям
    assert (await session.scalars(select(NotificationLog))).all() == []
    assert _card_item(ch) == expected
    assert f"Осталось в очереди: {len(rest)}" in ch.messages[(VICE, "500")]


async def test_not_in_league_denied_for_player(session, tenant_cfg):
    tenant, entries = await _imported(session, tenant_cfg)
    sidorov = await _player(session, tenant, SIDOROV)
    ch = FakeChannel()
    await handle(session, ch, _press(SIDOROV, {"a": "lnl", "p": sidorov.id, "e": entries["mlbl"].id}))
    assert ch.toasts[-1] == "Это доступно только руководителям команды"
    assert await roster.roster_status(session, tenant.id, sidorov.id, entries["mlbl"].id) == "unmatched"


async def test_card_with_three_candidates_fits_vk_keyboard(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    abl = entries["abl"]
    await _big_roster(session, abl, 4)
    await roster.import_chat_members(session, tenant.id, MEMBERS, [], None)
    petrov = await _player(session, tenant, PETROV)
    ids = [await _entry_id(session, abl, f"big{i}") for i in range(4)]
    item = roster.QueueItem(
        petrov,
        abl,
        await roster.entry_label(session, abl),
        [Candidate(r, Confidence.EXACT if i == 0 else Confidence.PROBABLE) for i, r in enumerate(ids)],
    )
    _, rows = await leader._card(session, tenant.id, item)
    _assert_vk_keyboard(rows)
    actions = [[b.payload["a"] for b in row] for row in rows]
    assert actions == [["lk"], ["lk"], ["lk"], ["ls"], ["ln", "lnl"], ["lx", "lskip"]]
    lnl = rows[4][1]
    assert lnl.label == "Не играет в лиге" and lnl.payload == {"a": "lnl", "p": petrov.id, "e": abl.id}


async def test_not_in_league_shown_in_my_data_and_fix(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    abl, mlbl = entries["abl"], entries["mlbl"]
    petrov = await _player(session, tenant, PETROV)
    captain = await _player(session, tenant, CAPTAIN)
    await roster.mark_not_in_league(session, tenant.id, petrov.id, mlbl.id, captain.id)
    await session.commit()
    abl_label = await roster.entry_label(session, abl)
    mlbl_label = await roster.entry_label(session, mlbl)

    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(PETROV, PETROV, "мои данные", is_chat=False, mentioned=False))
    text = ch.sent[-1][1]
    assert f"• {mlbl_label}: не играет в этой лиге" in text
    assert f"• {abl_label}: Петров Александр, №7" in text  # другая лига не затронута

    await handle(session, ch, _press(VICE, {"a": "lfix", "p": petrov.id}))
    overview = ch.messages[(VICE, "500")]
    assert f"• {mlbl_label}: не играет в этой лиге" in overview
    assert f"• {abl_label}: сопоставлен" in overview
    await handle(session, ch, _press(VICE, {"a": "le", "p": petrov.id, "e": mlbl.id}))
    assert "Сейчас: не играет в этой лиге" in ch.messages[(VICE, "500")]


async def test_fix_not_in_league_back_to_confirmed(session, tenant_cfg):
    """«исправить» → «Изменить: лига» → кандидат: not_in_league меняется на подтверждённую связь."""
    tenant, entries = await _ready(session, tenant_cfg)
    mlbl = entries["mlbl"]
    petrov = await _player(session, tenant, PETROV)
    ch = FakeChannel()
    await handle(session, ch, _press(VICE, {"a": "lnl", "p": petrov.id, "e": mlbl.id}))
    assert await roster.roster_status(session, tenant.id, petrov.id, mlbl.id) == "not_in_league"

    await handle(session, ch, _press(VICE, {"a": "le", "p": petrov.id, "e": mlbl.id}))
    lk = [b.payload for row in ch.buttons[(VICE, "500")] for b in row if b.payload["a"] == "lk"]
    m1 = await _entry_id(session, mlbl, "m1")
    assert [pl["r"] for pl in lk] == [m1]
    await handle(session, ch, _press(VICE, lk[0]))
    assert ch.toasts[-1] == "Связь подтверждена"
    assert await roster.roster_status(session, tenant.id, petrov.id, mlbl.id) == "in_roster"
    assert (await roster.roster_entry_for(session, tenant.id, petrov.id, mlbl.id)).id == m1


async def _match_for(session, entry) -> Match:
    rival = LeagueTeam(
        league_id=(await session.get(LeagueTeam, entry.league_team_id)).league_id,
        external_id="x",
        name="Соперник",
    )
    session.add(rival)
    await session.flush()
    start = utcnow() + timedelta(days=2)
    m = Match(
        tournament_id=entry.tournament_id,
        external_id="g1",
        home_team_id=entry.league_team_id,
        away_team_id=rival.id,
        match_date=start.date(),
        start_at=start,
        time_status="time_set",
        status="scheduled",
    )
    session.add(m)
    await session.commit()
    return m


async def test_going_not_in_league_warns_player_and_leaders(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    mlbl = entries["mlbl"]
    petrov = await _player(session, tenant, PETROV)
    captain = await _player(session, tenant, CAPTAIN)
    await roster.mark_not_in_league(session, tenant.id, petrov.id, mlbl.id, captain.id)
    m = await _match_for(session, mlbl)
    ch = FakeChannel()
    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, ch, chat, m)
    ref = MessageRef(CHAT, "101")

    await handle(session, ch, ButtonPress(CHAT, PETROV, ref, {"m": m.id, "v": "going"}, "e"))
    mine = [t for t in _dms(ch, PETROV) if "Буду" in t]
    assert len(mine) == 1 and "не играет в этой лиге" in mine[0]
    for lead in (CAPTAIN, VICE):
        alerts = [t for t in _dms(ch, lead) if "не играет в этой лиге" in t]
        assert len(alerts) == 1 and petrov.display_name in alerts[0]

    await handle(session, ch, ButtonPress(CHAT, PETROV, ref, {"m": m.id, "v": "not_going"}, "e"))
    await handle(session, ch, ButtonPress(CHAT, PETROV, ref, {"m": m.id, "v": "going"}, "e"))
    assert len([t for t in _dms(ch, PETROV) if "не играет в этой лиге" in t]) == 1


async def test_going_other_league_not_affected_by_not_in_league(session, tenant_cfg):
    """not_in_league в МЛБЛ не мешает «Буду» на матч АБЛ, где игрок сопоставлен."""
    tenant, entries = await _ready(session, tenant_cfg)
    petrov = await _player(session, tenant, PETROV)
    captain = await _player(session, tenant, CAPTAIN)
    await roster.mark_not_in_league(session, tenant.id, petrov.id, entries["mlbl"].id, captain.id)
    m = await _match_for(session, entries["abl"])
    ch = FakeChannel()
    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, ch, chat, m)
    ref = MessageRef(CHAT, "101")
    await handle(session, ch, ButtonPress(CHAT, PETROV, ref, {"m": m.id, "v": "going"}, "e"))
    assert not [p for p, _, _ in ch.sent if p != CHAT]


# ---------- «игроки»: список игроков со статусами ----------


async def _players_marked(session, tenant_cfg):
    """Все четыре метки: ✅ с номером и без, ⚠️, ➖, ❓; Друг Команды — «не игрок команды»; у Petrovich личка закрыта."""
    tenant, entries = await _ready(session, tenant_cfg)
    captain = await _player(session, tenant, CAPTAIN)
    sidorov = await _player(session, tenant, SIDOROV)
    petrovich = await _player(session, tenant, "12")
    friend = await _player(session, tenant, FRIEND)
    await roster.mark_not_in_roster(session, tenant.id, sidorov.id, entries["mlbl"].id, captain.id)
    await roster.mark_not_in_league(session, tenant.id, petrovich.id, entries["abl"].id, captain.id)
    await roster.mark_not_team_player(session, tenant.id, friend.id)
    await session.commit()
    return tenant, entries


def _row(cells, name, width):
    """Ожидаемая строка игрока: ячейки (значок, номер) по лигам через GAP, затем GAP и имя."""
    fs = leader.FIGURE_SPACE
    return leader.GAP.join(m + fs + n.rjust(width, fs) for m, n in cells) + leader.GAP + name


def _player_rows(lines):
    """Строки игроков: после шапки (счётчик, легенда, колонки, пустая строка) до следующей пустой строки."""
    assert lines[3] == ""
    rest = lines[4:]
    return rest[: rest.index("")] if "" in rest else rest


def _visible_len(text):
    """Длина без вариационного селектора U+FE0F (⚠️ = U+26A0 + U+FE0F — один значок)."""
    return len(text.replace("\ufe0f", ""))


def _assert_aligned(rows, names):
    """У всех строк префикс до имени одной длины."""
    prefixes = [row[: row.index(name)] for row, name in zip(rows, names, strict=True)]
    assert len({_visible_len(pr) for pr in prefixes}) == 1, prefixes
    assert all(pr.endswith(leader.GAP) for pr in prefixes)


async def test_players_list_marks_numbers_dm_and_not_team(session, tenant_cfg):
    tenant, _ = await _players_marked(session, tenant_cfg)
    msgs = await leader.players_list(session, tenant.id)
    assert len(msgs) == 1
    lines = msgs[0].split("\n")
    assert lines[0] == "Игроки команды: 5"  # «не игрок команды» не считается
    assert lines[1] == leader.PLAYERS_LEGEND
    assert lines[2] == "Колонки слева направо: АБЛ · МЛБЛ"
    rows = _player_rows(lines)
    w = 2  # самый длинный номер — «11»
    assert rows == [
        _row([("➖", ""), ("❓", "")], "Denis Petrovich 🔕", w),
        _row([("✅", "11"), ("⚠️", "")], "Evgeniy Sidorov", w),
        _row([("❓", ""), ("❓", "")], "Заместитель", w),
        _row([("✅", "1"), ("❓", "")], "Иван Капитанов", w),
        _row([("✅", "7"), ("✅", "")], "Саша Петров", w),  # в МЛБЛ номера нет — только ✅
    ]
    fs = leader.FIGURE_SPACE
    assert rows[3].startswith(f"✅{fs}{fs}1{leader.GAP}")  # «1» добит до ширины «11»
    assert all("• " not in r and " — " not in r and "№" not in r for r in rows)
    _assert_aligned(
        rows, ["Denis Petrovich", "Evgeniy Sidorov", "Заместитель", "Иван Капитанов", "Саша Петров"]
    )
    assert "Не игроки команды (1): Друг Команды" in lines
    assert "исправить" in lines[-1]


async def test_players_list_no_others_section_and_dm_closed(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    acc = (await session.execute(select(MessengerAccount).filter_by(external_user_id=PETROV))).scalar_one()
    acc.dm_allowed = False
    await session.commit()
    text = "\n".join(await leader.players_list(session, tenant.id))
    assert "Не игроки команды" not in text
    rows = _player_rows(text.split("\n"))
    petrov = next(r for r in rows if "Саша Петров" in r)
    captain = next(r for r in rows if "Иван Капитанов" in r)
    assert petrov.endswith(leader.GAP + "Саша Петров 🔕")
    assert captain.endswith(leader.GAP + "Иван Капитанов")  # личка открыта — без 🔕


@pytest.mark.parametrize("cmd", ["игроки", "Список", "исправить"])
async def test_players_command_for_leader(session, tenant_cfg, cmd):
    """«игроки», «список» и «исправить» без имени — список игроков (только руководителю)."""
    tenant, _ = await _players_marked(session, tenant_cfg)
    expected = await leader.players_list(session, tenant.id)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(VICE, VICE, cmd, is_chat=False, mentioned=False))
    assert _dms(ch, VICE) == expected
    assert expected[0].startswith("Игроки команды: 5")


@pytest.mark.parametrize("cmd", ["игроки", "список", "исправить"])
async def test_players_command_hidden_from_player(session, tenant_cfg, cmd):
    await _players_marked(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(SIDOROV, SIDOROV, cmd, is_chat=False, mentioned=False))
    assert len(ch.sent) == 1
    text = ch.sent[0][1]
    assert "Игроки команды" not in text and "Саша Петров" not in text
    assert "«игры»" in text and "Для руководителей" not in text


async def test_leader_hint_mentions_players_and_fix(session, tenant_cfg):
    await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(CAPTAIN, CAPTAIN, "помощь", is_chat=False, mentioned=False))
    text = ch.sent[-1][1]
    assert "«игроки»" in text and "«исправить" in text


async def test_players_list_long_split(session, tenant_cfg):
    """Длинный список — несколько сообщений, каждое ≤ 4096 и ≤ MESSAGE_LIMIT, ни одна строка не потеряна."""
    tenant, _ = await _ready(session, tenant_cfg)
    for i in range(120):
        session.add(Player(tenant_id=tenant.id, display_name=f"Игрок Длинноимённый-Вымышленный {i:03}"))
    await session.commit()
    msgs = await leader.players_list(session, tenant.id)
    assert len(msgs) > 1
    for m in msgs:
        assert len(m) <= leader.MESSAGE_LIMIT <= 4096
        assert m and not m.startswith("\n")
    joined = "\n".join(msgs).split("\n")
    assert joined[0] == "Игроки команды: 126"
    rows = [line for line in joined if "Игрок Длинноимённый" in line]
    assert len(rows) == 120
    assert all(
        r.endswith(leader.GAP + f"Игрок Длинноимённый-Вымышленный {i:03} 🔕") for i, r in enumerate(rows)
    )
    _assert_aligned(rows, ["Игрок Длинноимённый"] * 120)


async def _second_tenant(session, entries):
    """Вторая вымышленная команда сервиса: своя лига «ВЛБЛ» и выступление в той же команде АБЛ, что и у нашей.
    Громов — капитан, подтверждён в АБЛ (№7), личка открыта; Лесной — личка закрыта; Чужаков — «не игрок».
    Плюс «чужие» строки, которые не должны влиять на нашу команду: аккаунт и связь tenant B у игрока A."""
    b = Tenant(key="other-team", name="Другая команда")
    league = League(key="vlbl", name="ВЛБЛ", platform="mtgame", base_url="https://example.invalid")
    session.add_all([b, league])
    await session.flush()
    team = LeagueTeam(league_id=league.id, external_id="x1", name="Другие")
    tour = Tournament(league_id=league.id, external_id="t1", name="Сезон")
    session.add_all([team, tour])
    await session.flush()
    abl = entries["abl"]
    e_abl = TeamEntry(
        tenant_id=b.id, league_team_id=abl.league_team_id, tournament_id=abl.tournament_id, source_url="u"
    )
    e_own = TeamEntry(tenant_id=b.id, league_team_id=team.id, tournament_id=tour.id, source_url="u")
    gromov = Player(tenant_id=b.id, display_name="Олег Громов", role="captain")
    lesnoy = Player(tenant_id=b.id, display_name="Пётр Лесной")
    alien = Player(tenant_id=b.id, display_name="Гость Чужаков", active=False)
    session.add_all([e_abl, e_own, gromov, lesnoy, alien])
    await session.flush()
    session.add_all(
        [
            MessengerAccount(
                tenant_id=b.id, player_id=gromov.id, channel="vk", external_user_id="900", dm_allowed=True
            ),
            MessengerAccount(tenant_id=b.id, player_id=lesnoy.id, channel="vk", external_user_id="901"),
            MessengerAccount(
                tenant_id=b.id, player_id=alien.id, channel="vk", external_user_id="902", dm_allowed=True
            ),
            RosterLink(
                tenant_id=b.id,
                team_entry_id=e_abl.id,
                player_id=gromov.id,
                season_roster_entry_id=await _entry_id(session, abl, "a1"),
                status=RosterLinkStatus.CONFIRMED,
            ),
        ]
    )
    await session.commit()
    return b, gromov, lesnoy


B_NAMES = ("Олег Громов", "Пётр Лесной", "Гость Чужаков", "ВЛБЛ")
A_NAMES = (
    "Саша Петров",
    "Evgeniy Sidorov",
    "Denis Petrovich",
    "Иван Капитанов",
    "Заместитель",
    "Друг Команды",
)


async def test_players_list_isolated_between_tenants(session, tenant_cfg):
    """Мультитенантность «игроки»: в списке каждой команды — только её игроки, выступления и личка."""
    tenant, entries = await _players_marked(session, tenant_cfg)
    before = await leader.players_list(session, tenant.id)
    b, _, lesnoy = await _second_tenant(session, entries)
    petrovich = await _player(session, tenant, "12")  # в A личка закрыта, в МЛБЛ не сопоставлен
    # чужие строки на игроков A (tenant B) и на игрока B (tenant A) — не должны влиять
    session.add_all(
        [
            MessengerAccount(
                tenant_id=b.id, player_id=petrovich.id, channel="vk", external_user_id="950", dm_allowed=True
            ),
            RosterLink(
                tenant_id=b.id,
                team_entry_id=entries["mlbl"].id,
                player_id=petrovich.id,
                status=RosterLinkStatus.CONFIRMED,
            ),
            MessengerAccount(
                tenant_id=tenant.id,
                player_id=lesnoy.id,
                channel="vk",
                external_user_id="951",
                dm_allowed=True,
            ),
        ]
    )
    await session.commit()

    a_text = "\n".join(await leader.players_list(session, tenant.id))
    assert await leader.players_list(session, tenant.id) == before  # вторая команда ничего не изменила
    assert a_text.split("\n")[0] == "Игроки команды: 5"
    for name in B_NAMES:
        assert name not in a_text
    a_lines = a_text.split("\n")
    assert a_lines[2] == "Колонки слева направо: АБЛ · МЛБЛ"
    assert _row([("➖", ""), ("❓", "")], "Denis Petrovich 🔕", 2) in _player_rows(a_lines)
    marks = tuple(leader.PLAYERS_MARKS.values())
    for r in _player_rows(a_lines):  # ровно две ячейки: АБЛ и МЛБЛ
        assert sum(r.count(m) for m in marks) == 2

    b_lines = (await leader.players_list(session, b.id))[0].split("\n")
    b_text = "\n".join(b_lines)
    assert b_lines[0] == "Игроки команды: 2"
    assert b_lines[2] == "Колонки слева направо: АБЛ · ВЛБЛ"
    assert _player_rows(b_lines) == [  # ширина номера в B — своя: «7» → 1
        _row([("✅", "7"), ("❓", "")], "Олег Громов", 1),
        _row([("❓", ""), ("❓", "")], "Пётр Лесной 🔕", 1),
    ]
    assert "Не игроки команды (1): Гость Чужаков" in b_lines
    for name in A_NAMES:
        assert name not in b_text
    assert "МЛБЛ" not in b_text


async def test_players_command_leader_sees_only_own_tenant(session, tenant_cfg):
    """Руководитель A пишет «игроки» в личку — получает список своей команды без игроков B, и наоборот."""
    tenant, entries = await _players_marked(session, tenant_cfg)
    b, _, _ = await _second_tenant(session, entries)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(VICE, VICE, "игроки", is_chat=False, mentioned=False))
    a_text = "\n".join(_dms(ch, VICE))
    assert _dms(ch, VICE) == await leader.players_list(session, tenant.id)
    assert a_text.startswith("Игроки команды: 5")
    for name in B_NAMES:
        assert name not in a_text

    await handle(session, ch, IncomingMessage("900", "900", "игроки", is_chat=False, mentioned=False))
    b_text = "\n".join(_dms(ch, "900"))
    assert b_text.startswith("Игроки команды: 2")
    for name in A_NAMES:
        assert name not in b_text


def test_split_keeps_all_lines():
    lines = [f"строка {i} " + "x" * (i % 300) for i in range(400)] + ["", "конец"]
    chunks = leader._split(lines)
    assert len(chunks) > 1
    assert all(0 < len(c) <= leader.MESSAGE_LIMIT for c in chunks)
    assert "\n".join(chunks).split("\n") == lines
    assert leader._split([]) == []
    assert leader._split(["одна"]) == ["одна"]
