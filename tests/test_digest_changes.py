"""M4: отметка 🆕 в финальной сводке — изменения ответов и правок заявки на игру после сводки за сутки
(domain.md, «Жизненный цикл матча»: за 2 часа — «что поменялось — выделено»). Вымышленные имена."""

from datetime import timedelta

from sqlalchemy import select, update

from basket_assist.bot import leader
from basket_assist.services import digests, notify, weekly
from basket_assist.storage.tables import Attendance, NotificationLog, Player, utcnow
from tests.fakes import FakeChannel
from tests.test_digests import _full
from tests.test_leader_bot import FRIEND, PETROV, SIDOROV
from tests.test_roster import CAPTAIN, _player
from tests.test_weekly import D2, START, _ready, _to, _vote, _week_match

NEW = digests.NEW
CHANGED = "Изменилось с прошлой сводки: "


async def _after_digest24(session, tenant_cfg):
    """_full (АБЛ, нужно 2–3) + сводка за сутки ушла 2 ч назад, все голоса отданы раньше неё."""
    tenant, _, m = await _full(session, tenant_cfg)
    t0 = utcnow() - timedelta(hours=2)
    await session.execute(update(Attendance).values(updated_at=t0 - timedelta(hours=1)))
    session.add(
        NotificationLog(
            tenant_id=tenant.id, kind="digest24", dedup_key=weekly.cycle_key(m), match_id=m.id, sent_at=t0
        )
    )
    await session.commit()
    return tenant, m


async def _revote(session, m, player: Player, vote: str | None = None) -> None:
    a = (await session.scalars(select(Attendance).filter_by(match_id=m.id, player_id=player.id))).one()
    a.vote, a.updated_at = vote or a.vote, utcnow()
    await session.commit()


async def _elkin(session) -> Player:
    return (await session.scalars(select(Player).filter_by(display_name="Петя Ёлкин"))).one()


async def _apply_changes(session, tenant, m) -> None:
    """После суточной: Петрова убрал руководитель, Сидорова («Не буду») добавил,
    Друг Команды (⚠️) переголосовал, Ёлкин передумал — «Не буду»."""
    captain = await _player(session, tenant, CAPTAIN)
    for vk, removed in ((PETROV, True), (SIDOROV, False)):
        p = await _player(session, tenant, vk)
        await leader._edit_application(session, tenant.id, m.id, p.id, captain.id, removed=removed)
    await session.commit()
    await _revote(session, m, await _player(session, tenant, FRIEND))
    await _revote(session, m, await _elkin(session), "not_going")


def _body(text: str) -> list[str]:
    """Строки сводки от «🏀» до «Не ответили» включительно."""
    lines = text.split("\n")
    start = next(i for i, x in enumerate(lines) if x.startswith("🏀"))
    end = next(i for i, x in enumerate(lines) if x.startswith("Не ответили"))
    return lines[start : end + 1]


async def test_final_digest_marks_votes_and_edits_after_digest24(session, tenant_cfg):
    tenant, m = await _after_digest24(session, tenant_cfg)
    await _apply_changes(session, tenant, m)

    final, _ = await weekly.digest_message(session, tenant.id, m, "digest2")
    assert final.startswith(D2)
    assert _body(final)[4:] == [
        f"{CHANGED}4 (отмечены 🆕)",
        "",
        "Буду (5):",
        "  №1 Капитанов Иван",
        "  ⚠️ Denis Petrovich — не играет в этой лиге",
        f"  {NEW}⚠️ Друг Команды — нет в заявке на сезон",  # порядок по фамилии не сбит отметкой
        "  ⚠️ Заместитель — не сопоставлен с заявкой на сезон",
        f"  {NEW}✖️ Петров Александр — убран из заявки руководителем",
        "Добавлены руководителем (1):",
        f"  {NEW}№11 Сидоров Евгений",
        "В заявку на игру: 2 (нужно 2–3) ✅",
        f"Не буду (1): {NEW}Ёлкин Пётр",
        "Не ответили (0): —",
    ]

    daily, _ = await weekly.digest_message(session, tenant.id, m, "digest24")
    assert NEW not in daily and CHANGED not in daily, "в суточной отметок нет"
    assert _body(daily)[4:] == [x.replace(NEW, "") for x in _body(final)[6:]]


async def test_final_digest_marks_voted_row_and_keeps_order(session, tenant_cfg):
    """Отметка у строки заявки на игру (переотметился «Буду») и сортировка «Не буду» с отметками."""
    tenant, m = await _after_digest24(session, tenant_cfg)
    await _revote(session, m, await _player(session, tenant, PETROV))
    await _revote(session, m, await _elkin(session), "not_going")
    await _revote(session, m, await _player(session, tenant, SIDOROV))  # тот же «Не буду», но заново

    lines = _body((await weekly.digest_message(session, tenant.id, m, "digest2"))[0])
    assert lines[4] == f"{CHANGED}3 (отмечены 🆕)"
    assert lines[7:9] == ["  №1 Капитанов Иван", f"  {NEW}№77 Петров Александр"]
    assert lines[-2] == f"Не буду (2): {NEW}Ёлкин Пётр, {NEW}Сидоров Евгений"


async def test_final_digest_zero_changes(session, tenant_cfg):
    tenant, m = await _after_digest24(session, tenant_cfg)
    final, _ = await weekly.digest_message(session, tenant.id, m, "digest2")
    assert _body(final)[4:6] == [f"{CHANGED}0", ""]
    assert NEW not in final


async def test_final_digest_without_digest24_has_no_changes_line(session, tenant_cfg):
    tenant, _, m = await _full(session, tenant_cfg)
    final, _ = await weekly.digest_message(session, tenant.id, m, "digest2")
    assert CHANGED not in final and NEW not in final


async def test_match_digest_without_since_has_no_marks(session, tenant_cfg):
    tenant, m = await _after_digest24(session, tenant_cfg)
    await _apply_changes(session, tenant, m)
    plain = await digests.match_digest(session, tenant.id, m, "T")
    assert NEW not in plain and CHANGED not in plain


async def test_changed_since_only_own_tenant_and_match(session, tenant_cfg):
    tenant, m = await _after_digest24(session, tenant_cfg)
    since = utcnow() - timedelta(hours=2)
    assert await digests._changed_since(session, tenant.id, m.id, since) == set()
    petrov = await _player(session, tenant, PETROV)
    await _revote(session, m, petrov)
    assert await digests._changed_since(session, tenant.id, m.id, since) == {petrov.id}
    assert await digests._changed_since(session, tenant.id + 1, m.id, since) == set()
    assert await digests._changed_since(session, tenant.id, m.id + 1, since) == set()


async def test_sent_at_and_was_sent(session, tenant_cfg):
    tenant, m = await _after_digest24(session, tenant_cfg)
    at = await notify.sent_at(session, tenant.id, "digest24", weekly.cycle_key(m))
    assert at is not None and at.tzinfo is None
    assert await notify.was_sent(session, tenant.id, "digest24", weekly.cycle_key(m))
    assert await notify.sent_at(session, tenant.id, "digest2", weekly.cycle_key(m)) is None
    assert not await notify.was_sent(session, tenant.id, "digest2", weekly.cycle_key(m))
    assert await notify.sent_at(session, tenant.id + 1, "digest24", weekly.cycle_key(m)) is None


async def test_tick_final_digest_marks_change_after_daily(session, tenant_cfg):
    """Через tick: суточная без отметок, голос после неё — 🆕 в финальной, старый голос — без."""
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    ch = FakeChannel()
    await weekly.tick(session, ch, now=START - timedelta(hours=24))
    daily = _to(ch, CAPTAIN)[0][0]
    assert NEW not in daily and CHANGED not in daily
    # журнал пишет реальное время: «голос после сводки» — сдвигаем отметку отправки и старый голос назад
    hour_ago = utcnow() - timedelta(hours=1)
    await session.execute(update(NotificationLog).filter_by(kind="digest24").values(sent_at=hour_ago))
    await session.execute(update(Attendance).values(updated_at=hour_ago - timedelta(hours=1)))
    await session.commit()
    await _vote(session, tenant.id, m, await _player(session, tenant, SIDOROV), "not_going")
    await weekly.tick(session, ch, now=START - timedelta(hours=2))
    final = _to(ch, CAPTAIN)[2][0]
    assert final.startswith(D2)
    assert f"{CHANGED}1 (отмечены 🆕)" in final
    assert f"Не буду (1): {NEW}Сидоров Евгений" in final
    assert "  №7 Петров Александр" in final


async def test_woke_up_late_final_digest_without_changes_line(session, tenant_cfg):
    """Суточная не отправлялась (stale) — «прошлой сводки» не было, строки об изменениях быть не должно."""
    _, entries = await _ready(session, tenant_cfg)
    await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    await weekly.tick(session, ch, now=START - timedelta(hours=1))
    [(digest, _)] = _to(ch, CAPTAIN)
    assert digest.startswith(D2)
    assert CHANGED not in digest
