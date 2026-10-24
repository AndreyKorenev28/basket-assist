"""M4: заявка на игру с правками руководителя, кнопки под сводкой, «номер …», напоминание молчащим за 30 ч
(domain.md, «Заявка на игру», «Подключение игрока к боту»; architecture.md §5). Вымышленные имена."""

from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from basket_assist.bot import leader
from basket_assist.bot.handlers import handle
from basket_assist.channels.base import ButtonPress, ChannelError, IncomingMessage, MessageRef
from basket_assist.services import digests, polls, roster, weekly
from basket_assist.storage.tables import (
    ApplicationEntry,
    Attendance,
    MessengerAccount,
    Player,
    PollMessage,
    SeasonRosterEntry,
    TeamEntry,
    Tenant,
    utcnow,
)
from tests.fakes import FakeChannel
from tests.test_digests import _league, _publish
from tests.test_digests import _match as _rel_match
from tests.test_leader_bot import FRIEND, PETROV, SIDOROV, _ready, _second_tenant
from tests.test_roster import CAPTAIN, CHAT, VICE, _player
from tests.test_vk import _form, channel
from tests.test_weekly import REMIND, START, _to, _week_match

EXTRA = [  # ещё заявленные на сезон в АБЛ — для страниц выбора и поиска «номер»
    ("a11", "Иванов", "Иван", "21"),
    ("a12", "Тихонов", "Олег", "22"),
    ("a13", "Кузнецов", "Павел", None),
    ("a14", "Попов", "Игорь", "24"),
    ("a15", "Васильев", "Глеб", "25"),
]


async def _abl(session, tenant_cfg, extra: bool = False):
    """_ready + матч АБЛ через сутки с анонсом; extra — ещё 5 заявленных на сезон (всего 8)."""
    tenant, entries = await _ready(session, tenant_cfg)
    abl = entries["abl"]
    captain = await _player(session, tenant, CAPTAIN)
    if extra:
        for ext, last, first, num in EXTRA:
            p = Player(tenant_id=tenant.id, display_name=f"{first} {last}")
            r = SeasonRosterEntry(
                league_team_id=abl.league_team_id,
                tournament_id=abl.tournament_id,
                external_person_id=ext,
                last_name=last,
                first_name=first,
                number=num,
            )
            session.add_all([p, r])
            await session.flush()
            await roster.confirm(session, tenant.id, p.id, abl.id, r.id, captain.id)
    friend = await _player(session, tenant, FRIEND)
    await roster.mark_not_in_roster(session, tenant.id, friend.id, abl.id, captain.id)
    await session.commit()
    m = await _rel_match(session, abl, "g1", days=1)
    await _publish(session, m)
    return tenant, entries, m


async def _vote(session, tenant_id, m, player, vote="going"):
    row = (
        await session.execute(
            select(Attendance).filter_by(tenant_id=tenant_id, match_id=m.id, player_id=player.id)
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(Attendance(tenant_id=tenant_id, match_id=m.id, player_id=player.id, vote=vote))
    else:
        row.vote = vote
    await session.commit()


async def _edit(session, tenant_id, m, player, removed: bool):
    session.add(
        ApplicationEntry(
            tenant_id=tenant_id,
            match_id=m.id,
            player_id=player.id,
            source="captain",
            removed_by_captain=removed,
        )
    )
    await session.commit()


def _app(rows) -> list[tuple[str, str, bool]]:
    return [(r.name, r.number, r.added) for r in rows]


# ---------- digests.application ----------


@pytest.mark.parametrize(
    ("vote", "edit", "expected"),
    [
        (None, None, None),
        ("going", None, False),  # по голосу
        ("not_going", None, None),
        (None, "add", True),  # не ответил — добавлен руководителем
        ("going", "add", False),  # и голос, и правка — считается по голосу
        ("not_going", "add", True),  # «Не буду», но руководитель добавил
        (None, "remove", None),
        ("going", "remove", None),  # правка сильнее голоса
        ("not_going", "remove", None),
    ],
)
async def test_application_vote_x_edit(session, tenant_cfg, vote, edit, expected):
    tenant, _, m = await _abl(session, tenant_cfg)
    petrov = await _player(session, tenant, PETROV)
    if vote:
        await _vote(session, tenant.id, m, petrov, vote)
    if edit:
        await _edit(session, tenant.id, m, petrov, removed=edit == "remove")
    rows = await digests.application(session, tenant.id, m)
    if expected is None:
        assert rows == []
        assert await digests.application_text(session, tenant.id, m) is None
    else:
        assert _app(rows) == [("Петров Александр", "7", expected)]
        assert await digests.application_text(session, tenant.id, m) == "7 Петров Александр"


async def test_application_only_in_roster_sorted(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    friend = await _player(session, tenant, FRIEND)  # «нет в заявке на сезон»
    vice = await _player(session, tenant, VICE)  # не сопоставлен
    for p in (friend, vice):
        await _vote(session, tenant.id, m, p)
        await _edit(session, tenant.id, m, p, removed=False)
    await _vote(session, tenant.id, m, await _player(session, tenant, SIDOROV))
    await _edit(session, tenant.id, m, await _player(session, tenant, CAPTAIN), removed=False)
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    assert _app(await digests.application(session, tenant.id, m)) == [
        ("Капитанов Иван", "1", True),
        ("Петров Александр", "7", False),
        ("Сидоров Евгений", "11", False),
    ]


async def test_application_edits_isolated_by_tenant_and_match(session, tenant_cfg):
    tenant, entries, m = await _abl(session, tenant_cfg)
    b, gromov, _ = await _second_tenant(session, entries)
    other = await _rel_match(session, entries["abl"], "g2", days=2)
    petrov = await _player(session, tenant, PETROV)
    await _vote(session, tenant.id, m, petrov)
    await _edit(session, tenant.id, other, petrov, removed=True)  # правка другого матча
    session.add(
        ApplicationEntry(
            tenant_id=b.id, match_id=m.id, player_id=petrov.id, source="captain", removed_by_captain=True
        )
    )  # чужая строка
    await session.commit()
    assert _app(await digests.application(session, tenant.id, m)) == [("Петров Александр", "7", False)]


# ---------- текст сводки ----------


async def test_digest_block_with_removed_and_added(session, tenant_cfg):
    tenant, entries, m = await _abl(session, tenant_cfg)
    petrov = await _player(session, tenant, PETROV)
    sidorov = await _player(session, tenant, SIDOROV)
    await _vote(session, tenant.id, m, petrov)
    await _vote(session, tenant.id, m, sidorov, "not_going")
    await _vote(session, tenant.id, m, await _player(session, tenant, FRIEND))
    await _edit(session, tenant.id, m, petrov, removed=True)
    await _edit(session, tenant.id, m, sidorov, removed=False)
    league = await _league(session, entries["abl"])
    lines = (await digests.match_digest(session, tenant.id, m, "T")).split("\n")
    i = lines.index("Буду (2):")
    assert set(lines[i + 1 : i + 3]) == {
        "  ⚠️ Друг Команды — нет в заявке на сезон",
        "  ✖️ Петров Александр — убран из заявки руководителем",
    }
    assert lines[i + 3 : i + 8] == [
        "Добавлены руководителем (1):",
        "  №11 Сидоров Евгений",
        digests._size_note(1, league.min_players, league.max_players),
        "Не буду (0): —",  # добавленный в «Не буду» не дублируется
        "Не ответили (1): Капитанов Иван",
    ]
    assert await digests.application_text(session, tenant.id, m) == "11 Сидоров Евгений"


async def test_digest_block_added_silent_not_duplicated(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    await _edit(session, tenant.id, m, await _player(session, tenant, CAPTAIN), removed=False)
    text = await digests.match_digest(session, tenant.id, m, "T")
    assert "Буду (1):\n  №7 Петров Александр\nДобавлены руководителем (1):\n  №1 Капитанов Иван\n" in text
    assert "В заявку на игру: 2 " in text
    assert "Не ответили (1): Сидоров Евгений" in text


async def test_digest_message_number_hint(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    text, buttons = await weekly.digest_message(session, tenant.id, m, "digest2")
    assert text.endswith(f"\n\n{weekly.NUMBER_HINT}")
    assert [[(b.label, b.payload) for b in row] for row in buttons] == [
        [
            ("➕ Добавить", {"a": "aa", "m": m.id, "k": "digest2"}),
            ("➖ Убрать", {"a": "ar", "m": m.id, "k": "digest2"}),
        ],
        [("🔄 Обновить", {"a": "dg", "m": m.id, "k": "digest2"}), ("📋 Текст", {"a": "ac", "m": m.id})],
    ]


# ---------- кнопки aa / ar ----------

MSG = MessageRef(CAPTAIN, "500")


def _bp(user: str, payload: dict) -> ButtonPress:
    return ButtonPress(user, user, MessageRef(user, "500"), payload, "e")


def _rows(ch: FakeChannel, user: str = CAPTAIN):
    return [[(b.label, b.payload) for b in row] for row in ch.buttons[(user, "500")]]


async def test_add_pages_and_pick(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg, extra=True)
    captain = await _player(session, tenant, CAPTAIN)
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    ch = FakeChannel()
    ref = {"m": m.id, "k": "digest24"}
    back = ("← К сводке", {"a": "dg", **ref})

    await handle(session, ch, _bp(CAPTAIN, {"a": "aa", **ref}))
    assert ch.messages[(CAPTAIN, "500")] == "Кого добавить в заявку на игру? (только заявленные на сезон)"
    rows = _rows(ch)
    # вне заявки 7 заявленных (Петров уже в ней), не заявленные (Друг, Заместитель) не предлагаются
    assert [r[0][0] for r in rows[:-1]] == [
        "Васильев Глеб",
        "Иванов Иван",
        "Капитанов Иван",
        "Кузнецов Павел",
    ]
    assert rows[-1] == [back, ("Далее →", {"a": "aa", **ref, "pg": 1})]
    await handle(session, ch, _bp(CAPTAIN, {"a": "aa", **ref, "pg": 1}))
    rows = _rows(ch)
    assert [r[0][0] for r in rows[:-1]] == ["Попов Игорь", "Сидоров Евгений", "Тихонов Олег"]
    assert rows[-1] == [back], "последняя страница — без «Далее»"
    sidorov = await _player(session, tenant, SIDOROV)
    assert rows[1][0][1] == {"a": "aa1", **ref, "p": sidorov.id}

    await handle(session, ch, _bp(CAPTAIN, {"a": "aa1", **ref, "p": sidorov.id}))
    assert ch.toasts[-1] == "Добавлен в заявку"
    assert ch.messages[(CAPTAIN, "500")].startswith("📋 Сводка за сутки до игры")
    assert "Добавлены руководителем (1):\n  №11 Сидоров Евгений" in ch.messages[(CAPTAIN, "500")]
    row = (await session.execute(select(ApplicationEntry))).scalar_one()
    assert (row.tenant_id, row.match_id, row.player_id) == (tenant.id, m.id, sidorov.id)
    assert row.source == "captain" and not row.removed_by_captain and row.changed_by_player_id == captain.id
    assert ch.sent == [], "всё — правкой того же сообщения"


async def test_remove_then_add_again_and_vote_does_not_return(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    petrov = await _player(session, tenant, PETROV)
    vice = await _player(session, tenant, VICE)  # заместитель — тоже руководитель
    await _vote(session, tenant.id, m, petrov)
    ch = FakeChannel()
    ref = {"m": m.id, "k": "digest2"}

    await handle(session, ch, _bp(CAPTAIN, {"a": "ar", **ref}))
    assert ch.messages[(CAPTAIN, "500")] == "Кого убрать из заявки на игру?"
    assert _rows(ch) == [
        [("Петров Александр", {"a": "ar1", **ref, "p": petrov.id})],
        [("← К сводке", {"a": "dg", **ref})],
    ]
    await handle(session, ch, _bp(CAPTAIN, {"a": "ar1", **ref, "p": petrov.id}))
    assert ch.toasts[-1] == "Убран из заявки"
    assert ch.messages[(CAPTAIN, "500")].startswith("📋 Финальная сводка")
    assert "✖️ Петров Александр — убран из заявки руководителем" in ch.messages[(CAPTAIN, "500")]
    assert await digests.application(session, tenant.id, m) == []

    # Петров снова жмёт «Буду» в анонсе — правка руководителя сильнее
    await handle(
        session, ch, ButtonPress(CHAT, PETROV, MessageRef(CHAT, "101"), {"m": m.id, "v": "not_going"}, "e")
    )
    await handle(
        session, ch, ButtonPress(CHAT, PETROV, MessageRef(CHAT, "101"), {"m": m.id, "v": "going"}, "e")
    )
    assert await digests.application(session, tenant.id, m) == []

    # убранного можно вернуть: он снова в списке «добавить»; правка — та же строка
    await handle(session, ch, _bp(VICE, {"a": "aa", **ref}))
    assert ("Петров Александр", {"a": "aa1", **ref, "p": petrov.id}) in [r[0] for r in _rows(ch, VICE)]
    await handle(session, ch, _bp(VICE, {"a": "aa1", **ref, "p": petrov.id}))
    [row] = (await session.scalars(select(ApplicationEntry))).all()
    assert not row.removed_by_captain and row.changed_by_player_id == vice.id
    assert _app(await digests.application(session, tenant.id, m)) == [("Петров Александр", "7", False)]


async def test_empty_lists(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    ch = FakeChannel()
    ref = {"m": m.id, "k": "digest24"}
    back = [[("← К сводке", {"a": "dg", **ref})]]
    await handle(session, ch, _bp(CAPTAIN, {"a": "ar", **ref}))
    assert ch.messages[(CAPTAIN, "500")] == "В заявке на игру никого нет." and _rows(ch) == back
    for who in (CAPTAIN, PETROV, SIDOROV):  # все трое заявленных уже в заявке
        await _vote(session, tenant.id, m, await _player(session, tenant, who))
    await handle(session, ch, _bp(CAPTAIN, {"a": "aa", **ref}))
    assert ch.messages[(CAPTAIN, "500")] == "Добавить некого." and _rows(ch) == back


async def test_add_list_skips_inactive(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    sidorov = await _player(session, tenant, SIDOROV)
    sidorov.active = False
    await session.commit()
    ch = FakeChannel()
    await handle(session, ch, _bp(CAPTAIN, {"a": "aa", "m": m.id, "k": "digest24"}))
    assert [r[0][0] for r in _rows(ch)[:-1]] == ["Капитанов Иван", "Петров Александр"]


@pytest.mark.parametrize("a", ["aa", "ar", "aa1", "ar1"])
async def test_application_buttons_only_leaders(session, tenant_cfg, a):
    tenant, _, m = await _abl(session, tenant_cfg)
    petrov = await _player(session, tenant, PETROV)
    ch = FakeChannel()
    await handle(session, ch, _bp(PETROV, {"a": a, "m": m.id, "k": "digest24", "p": petrov.id}))
    assert ch.toasts == ["Это доступно только руководителям команды"]
    assert ch.edits == [] and ch.sent == []
    assert (await session.scalars(select(ApplicationEntry))).all() == []


@pytest.mark.parametrize("a", ["aa", "ar", "aa1", "ar1", "dg"])
async def test_application_buttons_foreign_match(session, tenant_cfg, a):
    """Матч другой команды сервиса или несуществующий: только «Матч не найден», в БД ничего не пишем."""
    tenant, entries = await _ready(session, tenant_cfg)
    b, _, _ = await _second_tenant(session, entries)
    own_b = (
        await session.scalars(
            select(TeamEntry).where(
                TeamEntry.tenant_id == b.id, TeamEntry.league_team_id != entries["abl"].league_team_id
            )
        )
    ).one()
    foreign = await _rel_match(session, own_b, "b1", days=1)
    petrov = await _player(session, tenant, PETROV)
    ch = FakeChannel()
    for mid in (foreign.id, 99999):
        await handle(session, ch, _bp(CAPTAIN, {"a": a, "m": mid, "k": "digest24", "p": petrov.id}))
    assert ch.toasts == ["Матч не найден", "Матч не найден"]
    assert ch.edits == [] and ch.sent == []
    assert (await session.scalars(select(ApplicationEntry))).all() == []


# ---------- «номер …» ----------


async def _override(session, tenant, vk: str, entry) -> str | None:
    p = await _player(session, tenant, vk)
    return (await roster._link(session, tenant.id, p.id, entry.id)).number_override


@pytest.mark.parametrize("args", ["", " ", " Петров", " 23", " 23 Петров", " Петров двадцать"])
async def test_number_command_hint(session, tenant_cfg, args):
    tenant, _, _ = await _abl(session, tenant_cfg)
    text, buttons = await leader.number_command(session, tenant.id, args)
    assert text.startswith("Напишите так: «номер Петров 23».") and buttons == []


async def test_number_command_single_sets_override(session, tenant_cfg):
    tenant, entries, m = await _abl(session, tenant_cfg)
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    text, buttons = await leader.number_command(session, tenant.id, " петров 23 абл")
    assert (
        text == "Номер записан: Петров Александр — №23 (АБЛ). В заявку на игру пойдёт он." and buttons == []
    )
    assert await _override(session, tenant, PETROV, entries["abl"]) == "23"
    assert await _override(session, tenant, PETROV, entries["mlbl"]) is None, "только в своей лиге"
    assert _app(await digests.application(session, tenant.id, m)) == [("Петров Александр", "23", False)]
    assert "  №23 Петров Александр" in await digests.match_digest(session, tenant.id, m, "T")
    assert await digests.application_text(session, tenant.id, m) == "23 Петров Александр"


async def test_number_command_by_display_name_and_yo(session, tenant_cfg):
    tenant, entries, _ = await _abl(session, tenant_cfg)
    text, _ = await leader.number_command(session, tenant.id, "Саша 9 АБЛ")  # имя из VK
    assert text.startswith("Номер записан: Петров Александр — №9")
    text, _ = await leader.number_command(session, tenant.id, "Евгений 5")
    assert text.startswith("Номер записан: Сидоров Евгений — №5 (АБЛ)")
    assert await _override(session, tenant, SIDOROV, entries["abl"]) == "5"


async def test_number_command_several_buttons(session, tenant_cfg):
    tenant, entries, _ = await _abl(session, tenant_cfg)
    petrov = await _player(session, tenant, PETROV)
    text, buttons = await leader.number_command(session, tenant.id, "Петров 23")  # АБЛ и МЛБЛ
    assert text == "Кому записать №23?"
    assert [[(b.label, b.payload) for b in row] for row in buttons] == [
        [("Петров Александр — АБЛ", {"a": "num", "p": petrov.id, "e": entries["abl"].id, "n": "23"})],
        [("Петров Александр — МЛБЛ", {"a": "num", "p": petrov.id, "e": entries["mlbl"].id, "n": "23"})],
    ]
    assert await _override(session, tenant, PETROV, entries["abl"]) is None, "пока не выбрали — не пишем"


async def test_number_command_too_many_and_not_found(session, tenant_cfg):
    tenant, entries, _ = await _abl(session, tenant_cfg, extra=True)
    text, buttons = await leader.number_command(session, tenant.id, "ов 5")  # 8 в АБЛ + Петров в МЛБЛ
    assert text == "Нашлось много игроков по «ов» — уточните фамилию." and buttons == []
    assert await leader.number_command(session, tenant.id, "Неизвестный 5") == (
        "Не нашёл «Неизвестный» среди заявленных на сезон.",
        [],
    )
    assert await leader.number_command(session, tenant.id, "Сидоров 5 МЛБЛ") == (
        "Не нашёл «Сидоров» среди заявленных на сезон в лиге «млбл».",
        [],
    )
    # «нет в заявке на сезон», не сопоставлен, «не игрок команды» — не находятся
    sidorov = await _player(session, tenant, SIDOROV)
    sidorov.active = False
    await session.commit()
    for q in ("Команды 5", "Заместитель 5", "Сидоров 5"):
        text, _ = await leader.number_command(session, tenant.id, q)
        assert text.startswith("Не нашёл"), q


async def test_number_via_dm_only_leaders(session, tenant_cfg):
    tenant, entries, _ = await _abl(session, tenant_cfg)
    ch = FakeChannel()
    await handle(
        session, ch, IncomingMessage(PETROV, PETROV, "номер Петров 23 АБЛ", is_chat=False, mentioned=False)
    )
    assert await _override(session, tenant, PETROV, entries["abl"]) is None
    await handle(
        session, ch, IncomingMessage(CAPTAIN, CAPTAIN, "Номер Петров 23 АБЛ", is_chat=False, mentioned=False)
    )
    assert ch.sent[-1][:2] == (
        CAPTAIN,
        "Номер записан: Петров Александр — №23 (АБЛ). В заявку на игру пойдёт он.",
    )
    assert await _override(session, tenant, PETROV, entries["abl"]) == "23"
    await handle(session, ch, IncomingMessage(CAPTAIN, CAPTAIN, "номер", is_chat=False, mentioned=False))
    assert ch.sent[-1][1].startswith("Напишите так:")


async def test_number_button(session, tenant_cfg):
    tenant, entries, _ = await _abl(session, tenant_cfg)
    b, gromov, _ = await _second_tenant(session, entries)
    petrov = await _player(session, tenant, PETROV)
    friend = await _player(session, tenant, FRIEND)
    abl = entries["abl"].id
    ch = FakeChannel()
    await handle(session, ch, _bp(CAPTAIN, {"a": "num", "p": petrov.id, "e": abl, "n": "23"}))
    assert ch.toasts[-1] == "Номер записан: №23"
    assert ch.messages[(CAPTAIN, "500")] == "Номер записан: №23. В заявку на игру пойдёт он."
    assert await _override(session, tenant, PETROV, entries["abl"]) == "23"

    # не заявлен на сезон в этой лиге / игрок другой команды сервиса — отказ
    for pid in (friend.id, gromov.id):
        await handle(session, ch, _bp(CAPTAIN, {"a": "num", "p": pid, "e": abl, "n": "5"}))
        assert ch.toasts[-1] == "Игрок не заявлен на сезон в этой лиге"
    assert (
        await roster._link(
            session,
            b.id,
            gromov.id,
            (
                await session.scalars(
                    select(TeamEntry.id).filter_by(
                        tenant_id=b.id, league_team_id=entries["abl"].league_team_id
                    )
                )
            ).one(),
        )
    ).number_override is None
    await handle(session, ch, _bp(PETROV, {"a": "num", "p": petrov.id, "e": abl, "n": "99"}))
    assert ch.toasts[-1] == "Это доступно только руководителям команды"
    assert await _override(session, tenant, PETROV, entries["abl"]) == "23"


# ---------- напоминание молчащим за 30 ч ----------


def _reminders(ch: FakeChannel) -> dict[str, list]:
    return {p: b for p, t, b in ch.sent if t.startswith(REMIND)}


async def _close_dm(session, *vk_ids):
    for vk in vk_ids:
        acc = (await session.execute(select(MessengerAccount).filter_by(external_user_id=vk))).scalar_one()
        acc.dm_allowed = False
    await session.commit()


def _after_poll():
    """Анонс _abl опубликован «сейчас» — напоминание не раньше чем через REMIND_AFTER_POLL (3 ч)."""
    return utcnow() + weekly.REMIND_AFTER_POLL


async def test_remind_late_poll_waits_3h(session, tenant_cfg):
    """Опрос запущен поздно (уже внутри окна 30 ч): напоминание не в ту же минуту, а через 3 ч после анонса."""
    tenant, entries = await _ready(session, tenant_cfg)
    await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    posted = START - timedelta(hours=20)  # время появилось поздно — анонс сразу
    await weekly.tick(session, ch, now=posted)
    assert len(_to(ch, CHAT)) == 1 and _reminders(ch) == {}, "анонс ушёл, напоминания в ту же минуту нет"
    await weekly.tick(session, ch, now=posted + timedelta(hours=2, minutes=59))
    assert _reminders(ch) == {}
    await weekly.tick(session, ch, now=posted + timedelta(hours=3))
    assert set(_reminders(ch)) == {CAPTAIN, PETROV, SIDOROV}
    await weekly.tick(session, ch, now=posted + timedelta(hours=4))
    assert len([1 for _, t, _ in ch.sent if t.startswith(REMIND)]) == 3, "один раз"


async def test_remind_late_poll_too_close_to_start(session, tenant_cfg):
    """Анонс за 4 ч до игры: +3 ч попадает после начала окна сводки за 2 ч — напоминания нет вовсе."""
    tenant, entries = await _ready(session, tenant_cfg)
    await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    for now in (
        START - timedelta(hours=4),
        START - timedelta(hours=2, minutes=1),
        START - timedelta(hours=1),
    ):
        await weekly.tick(session, ch, now=now)
    assert len(_to(ch, CHAT)) == 1 and _reminders(ch) == {}


async def test_remind_window_once_dm_with_vote_buttons(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    petrov = await _player(session, tenant, PETROV)
    await _vote(session, tenant.id, m, petrov, "not_going")  # ответил — не напоминаем
    ch = FakeChannel()
    await weekly.tick(session, ch, now=START - timedelta(hours=50))  # анонс заранее (как в среду)
    await weekly.tick(session, ch, now=START - timedelta(hours=30, minutes=1))
    assert _reminders(ch) == {} and len(_to(ch, CHAT)) == 1, "анонс есть, напоминания ещё нет"

    await weekly.tick(session, ch, now=START - timedelta(hours=30))
    rem = _reminders(ch)
    # только заявленные на сезон и активные: Заместитель (не сопоставлен) и Друг — нет
    assert set(rem) == {CAPTAIN, SIDOROV}
    assert rem[CAPTAIN] == polls.vote_buttons(m.id)
    [text] = [t for p, t, _ in ch.sent if p == SIDOROV]
    assert text == (
        "⏰ Напоминание: Соперник g1 — Westchester (АБЛ), сб 24.10, 12:00.\n"
        "Вы ещё не ответили — будете? Ответ можно поменять до начала игры."
    )
    assert ch.mention_sent == [], "у всех открыта личка — в беседу ничего"
    n = len(ch.sent)
    for now in (START - timedelta(hours=29), START - timedelta(hours=24), START - timedelta(hours=3)):
        await weekly.tick(session, ch, now=now)
    assert len([1 for _, t, _ in ch.sent if t.startswith(REMIND)]) == 2, "один раз"
    assert len(ch.sent) > n  # сводка за 24 ч ушла, напоминание — нет


@pytest.mark.parametrize("hours", [30.02, 2, 1])
async def test_remind_outside_window(session, tenant_cfg, hours):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    await _publish(session, m)
    ch = FakeChannel()
    await weekly._remind_silent(session, ch, entries["abl"], m, m.start_at - timedelta(hours=hours))
    assert ch.sent == []


async def test_remind_without_poll_then_later(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    now = m.start_at - timedelta(hours=29)
    await weekly._remind_silent(session, ch, entries["abl"], m, now)
    assert ch.sent == [], "без анонса не напоминаем и не помечаем"
    await _publish(session, m)
    poll = (await session.scalars(select(PollMessage))).one()
    poll.posted_at = (now - timedelta(hours=20)).replace(tzinfo=None)  # анонс в среду, а не «сейчас» по часам
    await session.commit()
    await weekly._remind_silent(session, ch, entries["abl"], m, now + timedelta(minutes=1))
    assert set(_reminders(ch)) == {CAPTAIN, PETROV, SIDOROV}


async def test_remind_dm_button_counts_vote(session, tenant_cfg):
    tenant, entries, m = await _abl(session, tenant_cfg)
    ch = FakeChannel()
    await weekly._remind_silent(session, ch, entries["abl"], m, _after_poll())
    [[going, _]] = _reminders(ch)[PETROV]
    await handle(session, ch, ButtonPress(PETROV, PETROV, MessageRef(PETROV, "777"), going.payload, "e"))
    assert ch.toasts[-1] == "Отмечено: Буду"
    petrov = await _player(session, tenant, PETROV)
    row = (
        await session.execute(select(Attendance).filter_by(match_id=m.id, player_id=petrov.id))
    ).scalar_one()
    assert row.tenant_id == tenant.id and row.vote == "going"
    assert _app(await digests.application(session, tenant.id, m)) == [("Петров Александр", "7", False)]


async def test_remind_mentions_only_chat_members(session, tenant_cfg):
    tenant, entries, m = await _abl(session, tenant_cfg)
    await _close_dm(session, CAPTAIN, SIDOROV)
    ch = FakeChannel()
    ch.members = [SIDOROV, PETROV, "999"]  # Капитанова в беседе нет (например, тестовая беседа)
    await weekly._remind_silent(session, ch, entries["abl"], m, _after_poll())
    assert set(_reminders(ch)) == {PETROV}, "личка — только с открытой"
    [(peer, text)] = ch.mention_sent
    assert peer == CHAT
    assert "Ещё не ответили: @11(Сидоров Евгений)." in text
    assert "@1(" not in text and "Капитанов" not in text
    assert "Отметьтесь в закреплённом анонсе" in text


async def test_remind_nobody_in_chat_no_message(session, tenant_cfg):
    tenant, entries, m = await _abl(session, tenant_cfg)
    await _close_dm(session, SIDOROV)
    ch = FakeChannel()
    ch.members = [CAPTAIN]
    await weekly._remind_silent(session, ch, entries["abl"], m, _after_poll())
    assert ch.mention_sent == [] and _to(ch, CHAT, reminders=True) == []


async def test_remind_chat_members_error(session, tenant_cfg):
    tenant, entries, m = await _abl(session, tenant_cfg)
    await _close_dm(session, SIDOROV)
    ch = FakeChannel()
    ch.members_error = ChannelError(917, "нет доступа к беседе")
    await weekly._remind_silent(session, ch, entries["abl"], m, _after_poll())
    assert ch.mention_sent == [] and _to(ch, CHAT, reminders=True) == []
    assert set(_reminders(ch)) == {CAPTAIN, PETROV}, "личка ушла"


async def test_remind_chat_send_error_is_logged(session, tenant_cfg):
    tenant, entries, m = await _abl(session, tenant_cfg)
    await _close_dm(session, SIDOROV)

    class Broken(FakeChannel):
        async def send_with_mentions(self, peer_id, text):
            raise ChannelError(None, "сбой VK")

    ch = Broken()
    ch.members = [SIDOROV]
    await weekly._remind_silent(session, ch, entries["abl"], m, _after_poll())  # не падает
    await weekly._remind_silent(session, ch, entries["abl"], m, _after_poll())
    assert set(_reminders(ch)) == {CAPTAIN, PETROV}


# ---------- VK: упоминания ----------


@respx.mock
async def test_vk_send_disable_mentions():
    route = respx.post("https://api.vk.com/method/messages.send").respond(
        json={"response": [{"peer_id": 2000000001, "message_id": 0, "conversation_message_id": 17}]}
    )
    async with httpx.AsyncClient() as client:
        ch = channel(client)
        await ch.send("2000000001", "обычное")
        await ch.send("2000000001", "с уведомлением", notify_mentions=True)
        assert await ch.send_with_mentions("2000000001", "напоминание") == MessageRef("2000000001", "17")
    forms = [_form(c) for c in route.calls]
    assert forms[0]["disable_mentions"] == "1"
    assert "disable_mentions" not in forms[1] and "disable_mentions" not in forms[2]
    assert forms[2]["message"] == "напоминание"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Сидоров Евгений", "[id11|Сидоров Евгений]"),
        ("Петров [Саша] | 7", "[id11|Петров (Саша) / 7]"),
        ("]|[", "[id11|)/(]"),
    ],
)
def test_vk_mention_escapes(name, expected):
    assert channel().mention("11", name) == expected


# ---------- «заявка» по запросу: карточка заявки на игру вне сводок ----------


def _app_buttons(m) -> list:
    ref = {"m": m.id, "k": "app"}
    return [
        [("➕ Добавить", {"a": "aa", **ref}), ("➖ Убрать", {"a": "ar", **ref})],
        [("🔄 Обновить", {"a": "dg", **ref}), ("📋 Текст", {"a": "ac", "m": m.id})],
    ]


def _plain(rows) -> list:
    return [[(b.label, b.payload) for b in row] for row in rows]


NO_ABL = "🏀 АБЛ: Westchester\nБлижайших матчей на сайте лиги пока нет."
NO_MLBL = "🏀 МЛБЛ: МИРЭА\nБлижайших матчей на сайте лиги пока нет."


async def test_lineup_card_for_league_with_poll_and_no_matches_block(session, tenant_cfg):
    """Сообщение на каждое выступление в порядке TeamEntry.id: АБЛ — карточка, МЛБЛ — матчей нет."""
    tenant, _, m = await _abl(session, tenant_cfg)
    [(card, buttons), (empty, none)] = await leader.lineup(session, tenant.id)
    assert [(card, buttons)] == await weekly.application_card(session, tenant.id, m)
    assert card.startswith(f"{weekly.APP_TITLE}\n\n🏀 АБЛ: Соперник g1 — Westchester")
    assert "Сводка" not in card and weekly.APPLICATION_HINT not in card
    assert _plain(buttons) == _app_buttons(m)
    assert (empty, none) == (NO_MLBL, None)


async def test_lineup_no_matches_each_league(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    assert await leader.lineup(session, tenant.id) == [(NO_ABL, None), (NO_MLBL, None)]


async def test_lineup_no_team_entries(session, tenant_cfg):
    await _ready(session, tenant_cfg)
    lonely = Tenant(key="lonely", name="Без выступлений")
    session.add(lonely)
    await session.commit()
    assert await leader.lineup(session, lonely.id) == [(digests.NO_MATCHES, None)]


async def test_lineup_match_without_time(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["mlbl"], "m1", days=2, with_time=False)
    [(abl, none), (text, buttons)] = await leader.lineup(session, tenant.id)
    assert (abl, none) == (NO_ABL, None) and buttons is None
    assert text.startswith("🏀 МЛБЛ: Соперник m1 — МИРЭА")
    assert "Время не назначено" in text
    assert text.endswith(f"\nЗнаете время — напишите «время {m.match_date:%d.%m} 19:00».")


async def test_lineup_time_set_poll_not_started(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    await _rel_match(session, entries["abl"], "g1", days=2)
    league = await _league(session, entries["abl"])
    [(text, buttons), _] = await leader.lineup(session, tenant.id)
    assert buttons is None and text.startswith("🏀 АБЛ: Соперник g1 — Westchester")
    assert text.endswith(f"Опрос ещё не запущен. Он запускается в среду перед игрой, в {league.poll_time}.")


async def test_lineup_only_nearest_match_per_league(session, tenant_cfg):
    """Ближайший матч без анонса, у следующего анонс есть — карточки нет (тот же матч, что в сводке);
    ближайший МЛБЛ без времени — следующий (с анонсом) не подставляем."""
    tenant, entries = await _ready(session, tenant_cfg)
    nearest = await _rel_match(session, entries["abl"], "g1", days=1)
    following = await _rel_match(session, entries["abl"], "g2", days=2)
    await _publish(session, following)
    await _rel_match(session, entries["mlbl"], "m1", days=1, with_time=False)
    next_ml = await _rel_match(session, entries["mlbl"], "m2", days=2)
    await _publish(session, next_ml)
    [(abl, b1), (mlbl, b2)] = await leader.lineup(session, tenant.id)
    assert b1 is None and "Соперник g1" in abl and "Опрос ещё не запущен." in abl
    assert b2 is None and "Соперник m1" in mlbl and "Знаете время" in mlbl
    await _publish(session, nearest)
    [(abl, b1), _] = await leader.lineup(session, tenant.id)
    assert abl.startswith(weekly.APP_TITLE) and "Соперник g1" in abl
    assert _plain(b1) == _app_buttons(nearest)


async def test_lineup_two_leagues_two_cards(session, tenant_cfg):
    tenant, entries, m = await _abl(session, tenant_cfg)
    m2 = await _rel_match(session, entries["mlbl"], "m1", days=2)
    await _publish(session, m2)
    [(abl, b1), (mlbl, b2)] = await leader.lineup(session, tenant.id)
    assert abl.startswith(weekly.APP_TITLE) and "🏀 АБЛ: Соперник g1" in abl
    assert mlbl.startswith(weekly.APP_TITLE) and "🏀 МЛБЛ: Соперник m1" in mlbl
    assert _plain(b1) == _app_buttons(m) and _plain(b2) == _app_buttons(m2)


async def test_digest_message_app_kind(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    text, buttons = await weekly.digest_message(session, tenant.id, m, "app")
    assert text.startswith(weekly.APP_TITLE) and "🆕" not in text
    assert _plain(buttons) == _app_buttons(m)


async def test_application_card_without_copy_text(session, tenant_cfg):
    """Карточка — одно сообщение даже с непустой заявкой: текст для копирования — по кнопке «📋»."""
    tenant, _, m = await _abl(session, tenant_cfg)
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    [(card, buttons)] = await weekly.application_card(session, tenant.id, m)
    assert weekly.APPLICATION_HINT not in card and _plain(buttons) == _app_buttons(m)


async def test_ap_button_sends_card_then_edit_keeps_app_title(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    ch = FakeChannel()
    press = ButtonPress(CAPTAIN, CAPTAIN, MessageRef(CAPTAIN, "50"), {"a": "ap", "m": m.id}, "e")
    await handle(session, ch, press)
    expected = await weekly.application_card(session, tenant.id, m)
    assert ch.sent == [(CAPTAIN, t, b) for t, b in expected] and len(ch.sent) == 1
    assert ch.edits == [] and ch.toasts == [""], "исходное (старое) сообщение не трогаем"

    card_id = next(mid for (peer, mid), t in ch.messages.items() if peer == CAPTAIN and t == expected[0][0])
    card = MessageRef(CAPTAIN, card_id)
    sidorov = await _player(session, tenant, SIDOROV)
    ref = {"m": m.id, "k": "app"}
    await handle(session, ch, ButtonPress(CAPTAIN, CAPTAIN, card, {"a": "aa", **ref}, "e"))
    assert ("Сидоров Евгений", {"a": "aa1", **ref, "p": sidorov.id}) in [
        r[0] for r in _plain(ch.buttons[(CAPTAIN, card_id)])
    ]
    await handle(session, ch, ButtonPress(CAPTAIN, CAPTAIN, card, {"a": "aa1", **ref, "p": sidorov.id}, "e"))
    assert ch.toasts[-1] == "Добавлен в заявку"
    redrawn = ch.messages[(CAPTAIN, card_id)]
    assert redrawn.startswith(weekly.APP_TITLE) and "Сидоров Евгений" in redrawn
    assert _plain(ch.buttons[(CAPTAIN, card_id)]) == _app_buttons(m)
    assert len(ch.sent) == 1, "правка — тем же сообщением"


async def test_ap_button_only_leaders(session, tenant_cfg):
    _, _, m = await _abl(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, _bp(PETROV, {"a": "ap", "m": m.id}))
    assert ch.toasts == ["Это доступно только руководителям команды"]
    assert ch.sent == [] and ch.edits == []


async def _foreign_match(session, entries):
    b, _, _ = await _second_tenant(session, entries)
    own_b = (
        await session.scalars(
            select(TeamEntry).where(
                TeamEntry.tenant_id == b.id, TeamEntry.league_team_id != entries["abl"].league_team_id
            )
        )
    ).one()
    return await _rel_match(session, own_b, "b1", days=1)


async def test_ap_button_foreign_or_missing_match(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    foreign = await _foreign_match(session, entries)
    ch = FakeChannel()
    for mid in (foreign.id, 99999):
        await handle(session, ch, _bp(CAPTAIN, {"a": "ap", "m": mid}))
    assert ch.toasts == ["Матч не найден", "Матч не найден"]
    assert ch.sent == [] and ch.edits == []


@pytest.mark.parametrize("cmd", ["заявка", "Заявка", "заявка на игру", "состав и заявка", "состав"])
async def test_application_command_for_leaders(session, tenant_cfg, cmd):
    """«заявка» и «состав и заявка» — одна команда: те же сообщения, что leader.lineup."""
    tenant, _, _ = await _abl(session, tenant_cfg)
    expected = await leader.lineup(session, tenant.id)
    for who in (CAPTAIN, VICE):
        ch = FakeChannel()
        await handle(session, ch, IncomingMessage(who, who, cmd, is_chat=False, mentioned=False))
        assert ch.sent == [(who, t, b) for t, b in expected]


async def test_application_command_no_match_text(session, tenant_cfg):
    await _ready(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(CAPTAIN, CAPTAIN, "заявка", is_chat=False, mentioned=False))
    assert ch.sent == [(CAPTAIN, NO_ABL, None), (CAPTAIN, NO_MLBL, None)]


# ---------- «📋 Текст» — текст для копирования (кнопка «ac») ----------


async def test_copy_button_sends_application_text(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    await _vote(session, tenant.id, m, await _player(session, tenant, SIDOROV))
    ch = FakeChannel()
    for who in (CAPTAIN, VICE):
        await handle(session, ch, _bp(who, {"a": "ac", "m": m.id}))
    assert ch.toasts == ["", ""] and ch.edits == []
    expected = "7 Петров Александр\n11 Сидоров Евгений"
    assert expected == await digests.application_text(session, tenant.id, m)
    assert ch.sent == [(CAPTAIN, expected, None), (VICE, expected, None)]


async def test_copy_button_reflects_leader_edits(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    petrov = await _player(session, tenant, PETROV)
    await _vote(session, tenant.id, m, petrov)
    ch = FakeChannel()
    await handle(session, ch, _bp(CAPTAIN, {"a": "ac", "m": m.id}))
    assert ch.sent[-1][1] == "7 Петров Александр"
    await _edit(session, tenant.id, m, petrov, removed=True)
    await _edit(session, tenant.id, m, await _player(session, tenant, SIDOROV), removed=False)
    await handle(session, ch, _bp(CAPTAIN, {"a": "ac", "m": m.id}))
    assert ch.sent[-1] == (CAPTAIN, "11 Сидоров Евгений", None)


async def test_copy_button_empty_application(session, tenant_cfg):
    _, _, m = await _abl(session, tenant_cfg)
    ch = FakeChannel()
    await handle(session, ch, _bp(CAPTAIN, {"a": "ac", "m": m.id}))
    assert ch.toasts == ["В заявке на игру пока никого нет"]
    assert ch.sent == [] and ch.edits == []


async def test_copy_button_foreign_or_missing_match(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    foreign = await _foreign_match(session, entries)
    ch = FakeChannel()
    for payload in ({"a": "ac", "m": foreign.id}, {"a": "ac", "m": 99999}, {"a": "ac"}):
        await handle(session, ch, _bp(CAPTAIN, payload))
    assert ch.toasts == ["Матч не найден"] * 3
    assert ch.sent == [] and ch.edits == []


async def test_copy_button_only_leaders(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    ch = FakeChannel()
    await handle(session, ch, _bp(PETROV, {"a": "ac", "m": m.id}))
    assert ch.toasts == ["Это доступно только руководителям команды"]
    assert ch.sent == [] and ch.edits == []
