"""M3: интерфейс руководителя в личке через handle() — передача прав, «исправить», новые участники,
«Это ошибка», ограничения клавиатуры VK (вымышленные имена)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from basket_assist.bot import leader
from basket_assist.bot.handlers import (
    CHAT_BUTTONS,
    GO_WORDS,
    HELP_BUTTONS_HINT,
    HELP_CHAT,
    HELP_DM,
    HELP_LEADER,
    HELP_RIGHTS,
    UNKNOWN,
    UNKNOWN_BUTTONS,
    WELCOME_DM,
    handle,
)
from basket_assist.channels.base import ButtonPress, ChannelError, IncomingMessage, MessageRef
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
NL = "\n"
LEADER_HELP = f"{HELP_DM}\n\n{HELP_LEADER}"  # помощь руководителю без строки «права»


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
    # ФИО из заявки на сезон («Петров Александр»), а не имя VK («Саша Петров»)
    assert ch.messages[(CAPTAIN, "500")] == "Передать права капитана: Петров Александр. На какой срок?"
    rows = ch.buttons[(CAPTAIN, "500")]
    assert sorted(b.payload["d"] for row in rows for b in row if b.payload["a"] == "g2") == [0, 3, 7, 14]
    assert rows[-1] == [leader.RIGHTS_CANCEL]
    await handle(session, ch, _press(CAPTAIN, {"a": "g2", "p": petrov.id, "d": 7}))
    rows = ch.buttons[(CAPTAIN, "500")]
    assert {b.payload["s"] for row in rows for b in row if b.payload["a"] == "g3"} == {0, 1}
    assert rows[-1] == [leader.RIGHTS_CANCEL]
    await handle(session, ch, _press(CAPTAIN, {"a": "g3", "p": petrov.id, "d": 7, "s": 1}))
    assert ch.toasts[-1] == "Права переданы"

    grant = (await session.execute(select(PermissionGrant))).scalar_one()
    assert grant.player_id == petrov.id and grant.granted_by_player_id == captain.id
    assert grant.valid_until is not None and grant.suppress_digests_for_grantor
    assert await leaders.is_leader(session, tenant.id, petrov.id)
    assert captain.id not in {p.id for p in await leaders.digest_recipients(session, tenant.id)}
    assert any("переданы права капитана" in t for t in _dms(ch, PETROV))
    chat_msgs = _dms(ch, CHAT)
    assert chat_msgs == [f"До {grant.valid_until:%d.%m} обязанности капитана исполняет Петров Александр."]
    assert "Саша" not in chat_msgs[0]
    # в меню появилась кнопка «Отозвать» — тоже с ФИО из заявки на сезон
    menu_text = ch.messages[(CAPTAIN, "500")]
    assert f"• Сейчас и. о.: Петров Александр, до {grant.valid_until:%d.%m}" in menu_text
    revoke = [b for row in ch.buttons[(CAPTAIN, "500")] for b in row if b.payload["a"] == "gr"]
    assert [(b.label, b.payload["g"]) for b in revoke] == [("Отозвать: Петров Александр", grant.id)]

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


async def _rights_pages(session, tenant_id) -> list[tuple[str, int]]:
    """Все кнопки кандидатов меню «права» по страницам, в порядке показа: (надпись, player_id)."""
    out, page = [], 0
    while True:
        _, rows = await leader.rights_menu(session, tenant_id, page)
        _assert_vk_keyboard(rows)
        payloads = [(b.label, b.payload) for row in rows for b in row]
        out += [(label, pl["p"]) for label, pl in payloads if pl["a"] == "g1"]
        nxt = [pl for _, pl in payloads if pl["a"] == "gm"]
        if not nxt:
            return out
        assert nxt[0]["pg"] == page + 1
        page = nxt[0]["pg"]


async def test_rights_menu_fio_sorted_by_last_name_across_pages(session, tenant_cfg):
    """Кандидаты — ФИО из заявки на сезон, по фамилии (без регистра, ё = е), пагинация — по этому порядку;
    порядок по имени VK заведомо другой."""
    tenant, entries = await _setup(session, tenant_cfg)
    first, second = sorted(entries.values(), key=lambda e: e.id)
    data = [  # имя VK (по алфавиту) → ФИО в обратном порядке фамилий
        ("Антон", "Яковлев", "Антон"),
        ("Борис", "Шубин", "Борис"),
        ("Вадим", "Ёлкин", "Вадим"),
        ("Глеб", "есаулов", "Глеб"),
        ("Денис", "Дронов", "Денис"),
        ("Егор", "Бочкин", "Егор"),
        ("Жора", "Бочкин", "Алексей"),  # однофамилец — по имени
    ]
    players = {}
    for i, (vk, last, fio_first) in enumerate(data):
        players[vk] = p = Player(tenant_id=tenant.id, display_name=vk)
        session.add(p)
        await session.flush()
        await _link_fio(session, tenant, first, p, f"r{i}", last, fio_first)
    only_second = Player(tenant_id=tenant.id, display_name="Аарон")  # сопоставлен только во второй лиге
    session.add(only_second)
    await session.flush()
    await _link_fio(session, tenant, second, only_second, "r2-1", "Гусев", "Пётр")
    await session.commit()

    labels = await _rights_pages(session, tenant.id)
    assert [label for label, _ in labels] == [
        "Бочкин Алексей",
        "Бочкин Егор",
        "Гусев Пётр",
        "Дронов Денис",
        "Ёлкин Вадим",
        "есаулов Глеб",
        "Шубин Борис",
        "Яковлев Антон",
    ]
    assert [pid for _, pid in labels][:2] == [players["Жора"].id, players["Егор"].id]
    assert len({pid for _, pid in labels}) == len(labels)  # без повторов между страницами

    # действующий и. о. — в тексте и на «Отозвать» с ФИО, из кандидатов пропадает, порядок остальных тот же
    captain = await _player(session, tenant, CAPTAIN)
    await leaders.grant_acting(session, tenant.id, captain.id, players["Денис"].id, None)
    await session.commit()
    text, rows = await leader.rights_menu(session, tenant.id)
    assert "• Сейчас и. о.: Дронов Денис, до отмены" in text
    assert [b.label for row in rows for b in row if b.payload["a"] == "gr"] == ["Отозвать: Дронов Денис"]
    labels = await _rights_pages(session, tenant.id)
    assert [label for label, _ in labels] == [
        "Бочкин Алексей",
        "Бочкин Егор",
        "Гусев Пётр",
        "Ёлкин Вадим",
        "есаулов Глеб",
        "Шубин Борис",
        "Яковлев Антон",
    ]


async def test_roster_fio_and_team_name(session, tenant_cfg):
    """roster_fio: только подтверждённая связь, первая лига по TeamEntry.id; team_name: иначе имя VK."""
    tenant, entries = await _setup(session, tenant_cfg)
    first, second = sorted(entries.values(), key=lambda e: e.id)
    p = Player(tenant_id=tenant.id, display_name="Ваня Иванов")
    session.add(p)
    await session.flush()
    assert await roster.roster_fio(session, tenant.id, p.id) is None
    assert await roster.team_name(session, tenant.id, p) == (("ваня иванов", p.id), "Ваня Иванов")

    # предложенная (не подтверждённая) связь — ещё не ФИО
    row = SeasonRosterEntry(
        league_team_id=first.league_team_id,
        tournament_id=first.tournament_id,
        external_person_id="q1",
        last_name="Иванов",
        first_name="Иван",
    )
    session.add(row)
    await session.flush()
    link = RosterLink(
        tenant_id=tenant.id,
        team_entry_id=first.id,
        player_id=p.id,
        season_roster_entry_id=row.id,
        status=RosterLinkStatus.SUGGESTED,
    )
    session.add(link)
    await _link_fio(session, tenant, second, p, "q2", "Ёжиков", "Иван")
    await session.flush()
    assert await roster.roster_fio(session, tenant.id, p.id) == ("Ёжиков", "Иван")  # вторая лига
    assert await roster.team_name(session, tenant.id, p) == (("ежиков иван", p.id), "Ёжиков Иван")

    link.status = RosterLinkStatus.CONFIRMED  # теперь подтверждена и в первой лиге — она важнее
    await session.flush()
    assert await roster.roster_fio(session, tenant.id, p.id) == ("Иванов", "Иван")
    assert (await roster.team_name(session, tenant.id, p))[1] == "Иванов Иван"

    # другая команда сервиса не видит связей этой
    other = Tenant(key="other_team", name="Другая команда")
    session.add(other)
    await session.flush()
    assert await roster.roster_fio(session, other.id, p.id) is None


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
        assert ch.sent[-1][1:] == (UNKNOWN, UNKNOWN_BUTTONS) and "Для руководителей" not in ch.sent[-1][1]
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


UNMATCHED_TITLE = "Не сопоставлены ни в одной лиге (имя из VK):"


def _header(leagues, width):
    """Ожидаемая строка заголовков: лига, добитая FIGURE_SPACE до ≈ ширины ячейки, через GAP; в конце «Игрок»."""
    fs = leader.FIGURE_SPACE
    heads = [n + fs * max(0, round(2.6 + width - 1.25 * len(n))) for n in leagues]
    return leader.GAP.join(heads) + leader.GAP + "Игрок"


def _blocks(lines):
    """(сопоставленные, не сопоставленные): шапка — счётчик, легенда, пустая строка, заголовки колонок."""
    assert lines[2] == ""
    rest = lines[4:]
    matched = rest[: rest.index("")] if "" in rest else rest
    rest = rest[len(matched) :]
    unmatched = []
    if rest[1:2] == [UNMATCHED_TITLE]:
        tail = rest[2:]
        unmatched = tail[: tail.index("")] if "" in tail else tail
    assert UNMATCHED_TITLE not in rest[len(unmatched) + 2 :]
    return matched, unmatched


def _player_rows(lines):
    """Все строки игроков: сначала сопоставленные, затем не сопоставленные ни в одной лиге."""
    matched, unmatched = _blocks(lines)
    return matched + unmatched


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
    w = 2  # самый длинный номер — «11»
    assert lines[3] == _header(["АБЛ", "МЛБЛ"], w)
    assert lines[3].endswith(leader.GAP + "Игрок") and "Колонки" not in msgs[0]
    matched, unmatched = _blocks(lines)
    # сопоставленные — ФИО из заявки на сезон (фамилия имя), по фамилии; по имени VK порядок был бы иным
    assert matched == [
        _row([("✅", "1"), ("❓", "")], "Капитанов Иван", w),
        _row([("✅", "7"), ("✅", "")], "Петров Александр", w),  # в МЛБЛ номера нет — только ✅
        _row([("✅", "11"), ("⚠️", "")], "Сидоров Евгений", w),
    ]
    # не сопоставленные ни в одной лиге — имя из VK, в порядке display_name
    assert unmatched == [
        _row([("➖", ""), ("❓", "")], "Denis Petrovich 🔕", w),
        _row([("❓", ""), ("❓", "")], "Заместитель", w),
    ]
    for vk_name in ("Саша Петров", "Evgeniy Sidorov", "Иван Капитанов"):
        assert vk_name not in msgs[0]
    rows = matched + unmatched
    fs = leader.FIGURE_SPACE
    assert matched[0].startswith(f"✅{fs}{fs}1{leader.GAP}")  # «1» добит до ширины «11»
    assert all("• " not in r and " — " not in r and "№" not in r for r in rows)
    _assert_aligned(  # ширина номера общая для обоих блоков
        rows, ["Капитанов", "Петров", "Сидоров", "Denis Petrovich", "Заместитель"]
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
    petrov = next(r for r in rows if "Петров Александр" in r)
    captain = next(r for r in rows if "Капитанов Иван" in r)
    assert petrov.endswith(leader.GAP + "Петров Александр 🔕")
    assert captain.endswith(leader.GAP + "Капитанов Иван")  # личка открыта — без 🔕


async def _link_fio(session, tenant, entry, player, pid, last, first, number=None):
    """Строка заявки на сезон в выступлении entry и подтверждённая связь игрока с ней."""
    row = SeasonRosterEntry(
        league_team_id=entry.league_team_id,
        tournament_id=entry.tournament_id,
        external_person_id=pid,
        last_name=last,
        first_name=first,
        number=number,
    )
    session.add(row)
    await session.flush()
    session.add(
        RosterLink(
            tenant_id=tenant.id,
            team_entry_id=entry.id,
            player_id=player.id,
            season_roster_entry_id=row.id,
            status=RosterLinkStatus.CONFIRMED,
        )
    )


async def test_players_list_fio_from_first_league_and_sort(session, tenant_cfg):
    """ФИО — из первого по TeamEntry.id выступления со связью; сортировка по (фамилия, имя) без учёта
    регистра и ё→е, затем по id; не сопоставленные — в конце по имени VK; выравнивание общее."""
    tenant, entries = await _setup(session, tenant_cfg)
    first, second = sorted(entries.values(), key=lambda e: e.id)
    names = {  # имя VK → данные; порядок по имени VK заведомо отличается от порядка по фамилии
        "Аня": ("Ёлкин", "Пётр"),  # ё → е: между «Ежов» и «Есенин»
        "Боря": ("есенин", "Олег"),  # строчная буква не уводит в конец
        "Вика": ("Ежов", "Ян"),
        "Гена": ("Абрамов", "Юрий"),
        "Дима": ("Абрамов", "Антон"),  # однофамилец: по имени
    }
    players = {}
    for i, (vk, (last, fio_first)) in enumerate(names.items()):
        players[vk] = pl = Player(tenant_id=tenant.id, display_name=vk)
        session.add(pl)
        await session.flush()
        await _link_fio(session, tenant, first, pl, f"x{i}", last, fio_first, str(i + 3))
    # два полных тёзки: порядок по player.id
    twin1 = Player(tenant_id=tenant.id, display_name="Яша")
    twin2 = Player(tenant_id=tenant.id, display_name="Юра")
    session.add_all([twin1, twin2])
    await session.flush()
    await _link_fio(session, tenant, first, twin1, "t1", "Сомов", "Лев")
    await _link_fio(session, tenant, first, twin2, "t2", "Сомов", "Лев")
    # сопоставлен в обеих лигах с разным написанием — берётся первая лига
    both = Player(tenant_id=tenant.id, display_name="Женя")
    only_second = Player(tenant_id=tenant.id, display_name="Зоя")  # связь только во второй лиге
    lonely = [Player(tenant_id=tenant.id, display_name=n) for n in ("Юлиан Неизвестный", "Аркадий Новенький")]
    session.add_all([both, only_second, *lonely])
    await session.flush()
    await _link_fio(session, tenant, first, both, "b1", "Семёнов", "Илья", "123")
    await _link_fio(session, tenant, second, both, "b2", "Semenov", "Ilya")
    await _link_fio(session, tenant, second, only_second, "s1", "Жукова", "Зоя")
    await session.commit()

    lines = (await leader.players_list(session, tenant.id))[0].split("\n")
    leagues = [await leader._league_name(session, e) for e in (first, second)]
    assert lines[3] == _header(leagues, 3)  # самый длинный номер — «123»
    matched, unmatched = _blocks(lines)
    tail = [r.split(leader.GAP)[-1] for r in matched]
    assert tail == [
        "Абрамов Антон 🔕",
        "Абрамов Юрий 🔕",
        "Ежов Ян 🔕",
        "Ёлкин Пётр 🔕",
        "есенин Олег 🔕",
        "Жукова Зоя 🔕",
        "Семёнов Илья 🔕",
        "Сомов Лев 🔕",
        "Сомов Лев 🔕",
    ]
    assert matched[-2:] == [  # тёзки — по id: первым идёт тот, кто создан раньше
        _row([("✅", ""), ("❓", "")], "Сомов Лев 🔕", 3),
        _row([("✅", ""), ("❓", "")], "Сомов Лев 🔕", 3),
    ]
    assert twin1.id < twin2.id
    assert _row([("✅", "123"), ("✅", "")], "Семёнов Илья 🔕", 3) in matched
    assert "Semenov" not in "\n".join(lines)
    assert _row([("❓", ""), ("✅", "")], "Жукова Зоя 🔕", 3) in matched  # ФИО из второй лиги
    assert unmatched == [
        _row([("❓", ""), ("❓", "")], f"{n} 🔕", 3)
        for n in (
            "Аркадий Новенький",
            "Заместитель",
            "Капитан",
            "Юлиан Неизвестный",
        )  # + руководители из конфига
    ]
    for vk in (*names, "Женя", "Зоя", "Яша", "Юра"):
        assert all(not r.endswith(leader.GAP + vk + " 🔕") for r in matched + unmatched)
    _assert_aligned(
        matched + unmatched,
        [t.removesuffix(" 🔕") for t in tail] + ["Аркадий", "Заместитель", "Капитан", "Юлиан"],
    )


async def test_players_list_all_matched_no_unmatched_block(session, tenant_cfg):
    """Все активные игроки сопоставлены хотя бы в одной лиге — блока «Не сопоставлены…» нет."""
    tenant, entries = await _ready(session, tenant_cfg)
    for vk in ("12", VICE, FRIEND):
        pl = await _player(session, tenant, vk)
        await roster.mark_not_team_player(session, tenant.id, pl.id)
    await session.commit()
    text = "\n".join(await leader.players_list(session, tenant.id))
    assert UNMATCHED_TITLE not in text
    matched, unmatched = _blocks(text.split("\n"))
    assert unmatched == [] and len(matched) == 3


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
    assert text == UNKNOWN and HELP_LEADER not in text


# ---------- «помощь» и неизвестные команды ----------

PLAYER_COMMANDS = [[("Игры", "игры", "primary"), ("Мои данные", "мои данные", "primary")]]
LEADER_ROW = [("Состав", "состав", "secondary"), ("Игроки", "игроки", "secondary")]
MATCHING = ("Сопоставление", "сопоставление", "secondary")
ACTING_COMMANDS = [*PLAYER_COMMANDS, LEADER_ROW, [MATCHING]]
CAPTAIN_COMMANDS = [*PLAYER_COMMANDS, LEADER_ROW, [MATCHING, ("Права", "права", "secondary")]]


def _commands(rows) -> list[list[tuple[str, str, str]]]:
    """Клавиатура подсказки → (подпись, команда, цвет); заодно проверяем ограничения VK."""
    assert rows
    _assert_vk_keyboard(rows)
    return [[(b.label, b.payload["c"], b.color) for b in row] for row in rows]


@pytest.mark.parametrize("cmd", ["помощь", "Команды", "help", "?", "!!"])
async def test_dm_help_for_player(session, tenant_cfg, cmd):
    await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(SIDOROV, SIDOROV, cmd, is_chat=False, mentioned=False))
    [(peer, text, buttons)] = ch.sent
    assert (peer, text) == (SIDOROV, HELP_DM + HELP_BUTTONS_HINT)
    assert _commands(buttons) == PLAYER_COMMANDS


@pytest.mark.parametrize("cmd", ["помощь", "команды", "?"])
async def test_dm_help_for_captain_and_vice_with_rights(session, tenant_cfg, cmd):
    await _ready(session, tenant_cfg)
    ch = FakeChannel()
    for who in (CAPTAIN, VICE):
        await handle(session, ch, IncomingMessage(who, who, cmd, is_chat=False, mentioned=False))
        _, text, buttons = ch.sent[-1]
        assert text == LEADER_HELP + HELP_RIGHTS + HELP_BUTTONS_HINT
        assert _commands(buttons) == CAPTAIN_COMMANDS
        assert "игроки" in text and "сопоставление" in text and "исправить" in text and "• права" in text


async def test_dm_help_for_acting_captain_without_rights(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, PETROV)
    await leaders.grant_acting(session, tenant.id, captain.id, petrov.id, None)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(PETROV, PETROV, "помощь", is_chat=False, mentioned=False))
    _, text, buttons = ch.sent[-1]
    assert text == LEADER_HELP + HELP_BUTTONS_HINT
    assert "• права" not in text
    assert _commands(buttons) == ACTING_COMMANDS


async def test_dm_start_is_welcome_plus_help(session, tenant_cfg):
    await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handle(
        session,
        ch,
        IncomingMessage(
            SIDOROV, SIDOROV, "Начать", is_chat=False, mentioned=False, payload={"command": "start"}
        ),
    )
    await handle(
        session, ch, IncomingMessage(CAPTAIN, CAPTAIN, "x", is_chat=False, mentioned=False, ref="chat")
    )
    (p1, t1, b1), (p2, t2, b2) = ch.sent
    assert (p1, t1) == (SIDOROV, WELCOME_DM + NL + HELP_DM + HELP_BUTTONS_HINT)
    assert _commands(b1) == PLAYER_COMMANDS
    assert (p2, t2) == (CAPTAIN, WELCOME_DM + NL + LEADER_HELP + HELP_RIGHTS + HELP_BUTTONS_HINT)
    assert _commands(b2) == CAPTAIN_COMMANDS


@pytest.mark.parametrize("who", [SIDOROV, CAPTAIN])
async def test_dm_unknown_command(session, tenant_cfg, who):
    await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(who, who, "когда тренировка", is_chat=False, mentioned=False))
    assert ch.sent == [(who, UNKNOWN, UNKNOWN_BUTTONS)]
    assert _commands(UNKNOWN_BUTTONS) == [[("Помощь", "помощь", "secondary")]]


@pytest.mark.parametrize("cmd", ["сопоставление", "права", "и. о.", "исправить Петров", "состав", "кто идёт"])
async def test_dm_leader_commands_unknown_for_player(session, tenant_cfg, cmd):
    await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(SIDOROV, SIDOROV, cmd, is_chat=False, mentioned=False))
    texts = _dms(ch, SIDOROV)
    assert texts == [UNKNOWN]


@pytest.mark.parametrize("cmd", ["помощь", "Команды", "help", "?", ""])
async def test_chat_help(session, tenant_cfg, cmd):
    await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(CHAT, SIDOROV, cmd, is_chat=True, mentioned=True))
    assert ch.sent == [(CHAT, HELP_CHAT, CHAT_BUTTONS)]
    assert _commands(CHAT_BUTTONS) == [[("Игры", "игры", "primary"), ("Перейти к боту", "бот", "primary")]]
    assert "в личке" in HELP_CHAT and HELP_LEADER not in HELP_CHAT
    assert "• бот — перейти в личку" in HELP_CHAT and "подключиться" not in HELP_CHAT


def _cmd_press(peer: str, user: str, command: str) -> ButtonPress:
    return ButtonPress(peer, user, MessageRef(peer, "500"), {"c": command}, "e")


async def _replies(session, event) -> tuple[list, FakeChannel]:
    ch = FakeChannel()
    await handle(session, ch, event)
    return ch.sent, ch


async def _assert_dm_buttons_like_text(session, who: str, rows) -> None:
    """Каждая кнопка подсказки в личке отвечает так же, как текст её команды; подсказку не редактирует."""
    for row in rows:
        for button in row:
            command = button.payload["c"]
            by_text, _ = await _replies(
                session, IncomingMessage(who, who, command, is_chat=False, mentioned=False)
            )
            by_button, ch = await _replies(session, _cmd_press(who, who, command))
            assert by_button == by_text, command
            assert by_button and all(peer == who for peer, _, _ in by_button)
            assert ch.toasts == [""] and ch.edits == []


@pytest.mark.parametrize("who", [SIDOROV, CAPTAIN])
async def test_dm_help_buttons_act_like_text(session, tenant_cfg, who):
    await _ready(session, tenant_cfg)
    sent, _ = await _replies(session, IncomingMessage(who, who, "помощь", is_chat=False, mentioned=False))
    await _assert_dm_buttons_like_text(session, who, sent[0][2])


async def test_dm_help_buttons_act_like_text_for_acting_captain(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, PETROV)
    await leaders.grant_acting(session, tenant.id, captain.id, petrov.id, None)
    sent, _ = await _replies(
        session, IncomingMessage(PETROV, PETROV, "помощь", is_chat=False, mentioned=False)
    )
    assert _commands(sent[0][2]) == ACTING_COMMANDS
    await _assert_dm_buttons_like_text(session, PETROV, sent[0][2])
    # подделанная кнопка «Права» у и. о. — тот же отказ, что и на текст
    sent, _ = await _replies(session, _cmd_press(PETROV, PETROV, "права"))
    assert sent == [(PETROV, "Передавать права может только капитан или заместитель.", None)]


@pytest.mark.parametrize("who", [SIDOROV, CAPTAIN])
async def test_dm_unknown_help_button(session, tenant_cfg, who):
    await _ready(session, tenant_cfg)
    sent, _ = await _replies(
        session, IncomingMessage(who, who, "абракадабра", is_chat=False, mentioned=False)
    )
    assert sent == [(who, UNKNOWN, UNKNOWN_BUTTONS)]
    await _assert_dm_buttons_like_text(session, who, UNKNOWN_BUTTONS)
    sent, _ = await _replies(session, _cmd_press(who, who, "помощь"))
    assert HELP_DM in sent[0][1] and sent[0][2]


@pytest.mark.parametrize(
    "command", ["игроки", "сопоставление", "права", "исправить Петров", "список", "состав", "кто идет"]
)
async def test_forged_command_button_from_player_is_unknown(session, tenant_cfg, command):
    await _ready(session, tenant_cfg)
    sent, ch = await _replies(session, _cmd_press(SIDOROV, SIDOROV, command))
    assert sent == [(SIDOROV, UNKNOWN, UNKNOWN_BUTTONS)]
    assert ch.toasts == [""]


@pytest.mark.parametrize("command", ["игры", "помощь", "мои данные", "игроки", "состав"])
async def test_chat_command_buttons_reply_in_chat_like_text(session, tenant_cfg, command):
    """Кнопки из подсказки беседы (и «Помощь» под «Не понял») отвечают в беседу, как обращение к боту."""
    await _ready(session, tenant_cfg)
    by_text, _ = await _replies(
        session, IncomingMessage(CHAT, CAPTAIN, command, is_chat=True, mentioned=True)
    )
    by_button, ch = await _replies(session, _cmd_press(CHAT, CAPTAIN, command))
    assert by_button == by_text
    assert [peer for peer, _, _ in by_button] == [CHAT], "ответ — в беседу, не в личку"
    assert ch.toasts == [""] and ch.edits == []
    if command in (
        "мои данные",
        "игроки",
        "состав",
    ):  # личные и руководительские данные в беседу не выводятся
        assert by_button == [(CHAT, UNKNOWN, UNKNOWN_BUTTONS)]
    if command == "помощь":
        assert by_button == [(CHAT, HELP_CHAT, CHAT_BUTTONS)]


async def test_vote_and_leader_buttons_not_treated_as_commands(session, tenant_cfg):
    """Голосование ("m") и кнопки руководителя ("a") идут своими путями, а не как команды."""
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match_for(session, entries["abl"])
    ch = FakeChannel()
    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, ch, chat, m)
    ref = MessageRef(CHAT, "101")
    before = len(ch.sent)
    await handle(session, ch, ButtonPress(CHAT, PETROV, ref, {"m": m.id, "v": "going"}, "e"))
    assert all(t != UNKNOWN for _, t, _ in ch.sent[before:])
    assert ch.edits and ch.edits[-1][0] == ref, "голос обновил анонс"
    assert "" not in ch.toasts, "голос не прошёл через кнопку-команду"
    ch = FakeChannel()
    await handle(session, ch, _press(VICE, {"a": "err"}))
    assert ch.toasts == ["Сообщил руководителям команды"]
    assert all(t != UNKNOWN for _, t, _ in ch.sent)


async def test_chat_unknown_and_unaddressed(session, tenant_cfg):
    await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(CHAT, SIDOROV, "что-то странное", is_chat=True, mentioned=True))
    assert ch.sent == [(CHAT, UNKNOWN, UNKNOWN_BUTTONS)]
    for text in ("помощь", "игры", "что-то странное", "?"):
        await handle(session, ch, IncomingMessage(CHAT, SIDOROV, text, is_chat=True, mentioned=False))
    assert _dms(ch, CHAT) == [UNKNOWN], "без обращения к боту сообщения беседы игнорируются"


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


B_NAMES = ("Олег Громов", "Пётр Лесной", "Гость Чужаков", "ВЛБЛ")  # «Олег Громов» в B показан по заявке
A_NAMES = (
    "Саша Петров",
    "Evgeniy Sidorov",
    "Denis Petrovich",
    "Иван Капитанов",
    "Капитанов Иван",
    "Сидоров Евгений",
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
    assert a_lines[3] == _header(["АБЛ", "МЛБЛ"], 2)
    # чужая подтверждённая связь (tenant B) не делает Petrovich сопоставленным в A
    assert _row([("➖", ""), ("❓", "")], "Denis Petrovich 🔕", 2) in _blocks(a_lines)[1]
    marks = tuple(leader.PLAYERS_MARKS.values())
    for r in _player_rows(a_lines):  # ровно две ячейки: АБЛ и МЛБЛ
        assert sum(r.count(m) for m in marks) == 2

    b_lines = (await leader.players_list(session, b.id))[0].split("\n")
    b_text = "\n".join(b_lines)
    assert b_lines[0] == "Игроки команды: 2"
    assert b_lines[3] == _header(["АБЛ", "ВЛБЛ"], 1)
    assert _blocks(b_lines) == (  # ширина номера в B — своя: «7» → 1
        [_row([("✅", "7"), ("❓", "")], "Петров Александр", 1)],  # Громов связан со строкой a1 заявки АБЛ
        [_row([("❓", ""), ("❓", "")], "Пётр Лесной 🔕", 1)],
    )
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


# ---------- «состав»: состав на матч (M4) ----------

LINEUP_BUTTONS = [[("Обновить", {"a": "lineup"}, "secondary")]]


def _lineup_buttons(rows):
    _assert_vk_keyboard(rows)
    return [[(b.label, b.payload, b.color) for b in row] for row in rows]


def test_help_leader_starts_with_lineup():
    assert HELP_LEADER.startswith(
        "Для руководителей команды:\n\n• состав — кто отметился на ближайшую игру в каждой лиге"
    )


@pytest.mark.parametrize("cmd", ["состав", "Состав", "кто идёт", "Кто идет?"])
async def test_lineup_text_command_for_leaders(session, tenant_cfg, cmd):
    tenant, entries = await _ready(session, tenant_cfg)
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, PETROV)
    await leaders.grant_acting(session, tenant.id, captain.id, petrov.id, None)
    m = await _match_for(session, entries["abl"])
    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, FakeChannel(), chat, m)
    for who in (CAPTAIN, VICE, PETROV):  # капитан, заместитель, и. о.
        ch = FakeChannel()
        await handle(session, ch, IncomingMessage(who, who, cmd, is_chat=False, mentioned=False))
        [(peer, text, buttons)] = ch.sent
        assert peer == who
        assert text.startswith("🏀 АБЛ: Westchester — Соперник") and "Не ответили (3):" in text
        assert _lineup_buttons(buttons) == LINEUP_BUTTONS


async def test_lineup_refresh_edits_message(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match_for(session, entries["abl"])
    ch = FakeChannel()
    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, ch, chat, m)
    await handle(session, ch, _cmd_press(CAPTAIN, CAPTAIN, "состав"))  # кнопка «Состав» из подсказки
    first = ch.sent[-1][1]
    assert "Буду (0):" in first and ch.edits == []

    await handle(
        session, ch, ButtonPress(CHAT, SIDOROV, MessageRef(CHAT, "101"), {"m": m.id, "v": "going"}, "e")
    )
    sent_before = len(ch.sent)
    ref = MessageRef(CAPTAIN, "500")
    await handle(session, ch, ButtonPress(CAPTAIN, CAPTAIN, ref, {"a": "lineup"}, "e"))
    assert ch.toasts[-1] == "Обновлено"
    assert ch.edits[-1][0] == ref, "«Обновить» редактирует то же сообщение"
    updated = ch.messages[(CAPTAIN, "500")]
    assert "Буду (1):" in updated and "  №11 Сидоров Евгений" in updated
    assert _lineup_buttons(ch.buttons[(CAPTAIN, "500")]) == LINEUP_BUTTONS
    assert len(ch.sent) == sent_before, "нового сообщения нет"


async def test_lineup_refresh_falls_back_to_send(session, tenant_cfg):
    await _ready(session, tenant_cfg)
    ch = FakeChannel(editable=False)
    await handle(session, ch, _press(CAPTAIN, {"a": "lineup"}))
    assert ch.toasts == ["Обновлено"] and ch.edits == []
    [(peer, text, buttons)] = ch.sent
    assert peer == CAPTAIN and "Ближайших матчей на сайте лиги пока нет." in text
    assert _lineup_buttons(buttons) == LINEUP_BUTTONS


async def test_lineup_denied_for_player(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match_for(session, entries["abl"])
    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, FakeChannel(), chat, m)
    for event in (
        IncomingMessage(SIDOROV, SIDOROV, "состав", is_chat=False, mentioned=False),
        IncomingMessage(SIDOROV, SIDOROV, "кто идёт", is_chat=False, mentioned=False),
        _cmd_press(SIDOROV, SIDOROV, "состав"),
    ):
        sent, _ = await _replies(session, event)
        assert sent == [(SIDOROV, UNKNOWN, UNKNOWN_BUTTONS)]
    sent, ch = await _replies(session, _press(SIDOROV, {"a": "lineup"}))
    assert ch.toasts == ["Это доступно только руководителям команды"]
    assert sent == [] and ch.edits == []


async def test_lineup_not_in_chat_even_for_captain(session, tenant_cfg):
    await _ready(session, tenant_cfg)
    for cmd in ("состав", "кто идёт"):
        sent, _ = await _replies(session, IncomingMessage(CHAT, CAPTAIN, cmd, is_chat=True, mentioned=True))
        assert sent == [(CHAT, UNKNOWN, UNKNOWN_BUTTONS)]


# ---------- «Перейти к боту» из беседы ----------

LINK = "https://vk.me/club1?ref=chat"


@pytest.mark.parametrize("cmd", GO_WORDS)
async def test_chat_go_words_text_reply_with_link(session, tenant_cfg, cmd):
    """Текстовая команда в беседе — ответ в беседу со ссылкой на личку (ref=chat)."""
    await _ready(session, tenant_cfg)
    sent, ch = await _replies(session, IncomingMessage(CHAT, SIDOROV, cmd, is_chat=True, mentioned=True))
    [(peer, text, _)] = sent
    assert peer == CHAT and LINK in text
    assert ch.toasts == []


@pytest.mark.parametrize(
    ("who", "commands"), [(SIDOROV, PLAYER_COMMANDS), (CAPTAIN, CAPTAIN_COMMANDS), (VICE, CAPTAIN_COMMANDS)]
)
async def test_chat_go_button_sends_menu_to_dm_by_role(session, tenant_cfg, who, commands):
    await _ready(session, tenant_cfg)
    sent, ch = await _replies(session, _cmd_press(CHAT, who, "бот"))
    [(peer, _, buttons)] = sent
    assert peer == who, "меню — в личку, в беседу ничего"
    assert _commands(buttons) == commands
    assert ch.toasts == [""] and ch.links == [LINK] and ch.edits == []


async def test_chat_go_button_acting_captain_menu_without_rights(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, PETROV)
    await leaders.grant_acting(session, tenant.id, captain.id, petrov.id, None)
    sent, ch = await _replies(session, _cmd_press(CHAT, PETROV, "бот"))
    [(peer, text, buttons)] = sent
    assert peer == PETROV and _commands(buttons) == ACTING_COMMANDS and "• права" not in text
    assert ch.links == [LINK]


@pytest.mark.parametrize("cmd", GO_WORDS)
async def test_chat_go_button_every_go_word(session, tenant_cfg, cmd):
    await _ready(session, tenant_cfg)
    sent, ch = await _replies(session, _cmd_press(CHAT, SIDOROV, cmd))
    assert [peer for peer, _, _ in sent] == [SIDOROV]
    assert ch.links == [LINK]


async def test_chat_go_button_dm_closed_or_no_account(session, tenant_cfg):
    """Личка не открыта или аккаунта нет — в личку ничего, только ответ со ссылкой."""
    await _ready(session, tenant_cfg)
    acc = (await session.execute(select(MessengerAccount).filter_by(external_user_id=SIDOROV))).scalar_one()
    acc.dm_allowed = False
    await session.commit()
    for who in (SIDOROV, "999"):
        sent, ch = await _replies(session, _cmd_press(CHAT, who, "бот"))
        assert sent == [] and ch.toasts == [""] and ch.links == [LINK]
    stranger = await session.execute(select(MessengerAccount).filter_by(external_user_id="999"))
    assert stranger.scalar_one_or_none() is None, "нажатие «Перейти к боту» не заводит аккаунт"


class _DmDenied(FakeChannel):
    async def send(self, peer_id, text, buttons=None):
        if peer_id != CHAT:
            raise ChannelError(901, "Can't send messages for users without permission")
        return await super().send(peer_id, text, buttons)


async def test_chat_go_button_dm_error_is_logged_not_fatal(session, tenant_cfg, caplog):
    await _ready(session, tenant_cfg)
    ch = _DmDenied()
    await handle(session, ch, _cmd_press(CHAT, SIDOROV, "бот"))
    assert ch.sent == [] and ch.links == [LINK]
    assert "901" in caplog.text


# ---------- «Отмена» в передаче прав ----------


async def _many_matched(session, tenant, entry, n: int) -> list[Player]:
    captain = await _player(session, tenant, CAPTAIN)
    out = []
    for i in range(n):
        p = Player(tenant_id=tenant.id, display_name=f"Запасной{i}")
        r = SeasonRosterEntry(
            league_team_id=entry.league_team_id,
            tournament_id=entry.tournament_id,
            external_person_id=f"c{i}",
            last_name="Запасной",
            first_name=f"Игрок{i}",
        )
        session.add_all([p, r])
        await session.flush()
        await roster.confirm(session, tenant.id, p.id, entry.id, r.id, captain.id)
        out.append(p)
    await session.commit()
    return out


async def test_rights_menu_last_row_is_cancel(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    _, rows = await leader.rights_menu(session, tenant.id)
    _assert_vk_keyboard(rows)
    assert rows[-1] == [leader.RIGHTS_CANCEL]
    assert leader.RIGHTS_CANCEL.payload == {"a": "gx"} and leader.RIGHTS_CANCEL.label == "Отмена"


@pytest.mark.parametrize("grants", [0, 1, 2])
async def test_rights_menu_cancel_and_next_share_last_row(session, tenant_cfg, grants):
    """Много кандидатов: последний ряд [Отмена, Далее →], кандидатов на странице 6 − «Отозвать» − 1;
    на последней странице — одна «Отмена»."""
    tenant, entries = await _ready(session, tenant_cfg)
    extra = await _many_matched(session, tenant, entries["abl"], 10)
    captain = await _player(session, tenant, CAPTAIN)
    for p in extra[:grants]:
        await leaders.grant_acting(session, tenant.id, captain.id, p.id, None)
    await session.commit()
    _, rows = await leader.rights_menu(session, tenant.id)
    _assert_vk_keyboard(rows)
    assert [b.payload["a"] for b in rows[-1]] == ["gx", "gm"]
    assert sum(b.payload["a"] == "g1" for row in rows for b in row) == 6 - grants - 1
    page = 0
    while True:
        _, rows = await leader.rights_menu(session, tenant.id, page)
        _assert_vk_keyboard(rows)
        nxt = [b.payload for b in rows[-1] if b.payload["a"] == "gm"]
        if not nxt:
            break
        page = nxt[0]["pg"]
    assert rows[-1] == [leader.RIGHTS_CANCEL]


@pytest.mark.parametrize("who", [CAPTAIN, VICE])
async def test_rights_cancel_changes_nothing(session, tenant_cfg, who):
    tenant, _ = await _ready(session, tenant_cfg)
    petrov = await _player(session, tenant, PETROV)
    ch = FakeChannel()
    await handle(session, ch, _press(who, {"a": "g1", "p": petrov.id}))
    await handle(session, ch, _press(who, {"a": "g2", "p": petrov.id, "d": 7}))
    await handle(session, ch, _press(who, {"a": "gx"}))
    assert ch.toasts[-1] == "Отменено"
    assert ch.messages[(who, "500")] == "Передача прав отменена, ничего не изменилось."
    rows = ch.buttons[(who, "500")]
    assert [[(b.label, b.payload) for b in row] for row in rows] == [[("Открыть снова", {"a": "gm"})]]
    assert (await session.scalars(select(PermissionGrant))).all() == []
    assert not await leaders.is_leader(session, tenant.id, petrov.id)
    assert _dms(ch, CHAT) == [] and _dms(ch, PETROV) == []
    await handle(session, ch, _press(who, {"a": "gm"}))  # «Открыть снова» — снова меню прав
    assert "Кому передать?" in ch.messages[(who, "500")]


async def test_rights_cancel_denied_for_acting_and_player(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, PETROV)
    await leaders.grant_acting(session, tenant.id, captain.id, petrov.id, None)
    ch = FakeChannel()
    await handle(session, ch, _press(PETROV, {"a": "gx"}))
    assert ch.toasts[-1] == "Передавать права может только капитан или заместитель"
    await handle(session, ch, _press(SIDOROV, {"a": "gx"}))
    assert ch.toasts[-1] == "Это доступно только руководителям команды"
    assert ch.edits == []
