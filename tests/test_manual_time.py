"""M4: «время 25.10 18:30» — время матча от руководителя, если на сайте лиги его нет
(domain.md, «Жизненный цикл матча»). Вымышленные имена."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from basket_assist.bot import leader
from basket_assist.bot.handlers import UNKNOWN, handle
from basket_assist.channels.base import Button, ButtonPress, IncomingMessage, MessageRef
from basket_assist.domain.models import TimeStatus
from basket_assist.services import weekly
from basket_assist.storage.tables import Match, PollMessage, TeamEntry
from tests.fakes import FakeChannel
from tests.test_digests import _match
from tests.test_leader_bot import SIDOROV, _second_tenant
from tests.test_roster import CAPTAIN, CHAT, _player
from tests.test_weekly import NO_TIME, _ready, _to

MSK = ZoneInfo("Europe/Moscow")
HINT = "Напишите так: «время 25.10 18:30» — дата матча и время начала."


def _utc(m: Match, hh: int, mm: int) -> datetime:
    """hh:mm по Москве в день матча → UTC без tzinfo (как хранится start_at)."""
    return datetime.combine(m.match_date, time(hh, mm), MSK).astimezone(UTC).replace(tzinfo=None)


def _cmd(m: Match, hm: str = "18:30") -> str:
    return f" {m.match_date:%d.%m} {hm}"


async def _no_time_match(session, tenant_cfg, days: float = 3):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "g1", days=days, with_time=False)
    return tenant, entries, m


async def test_single_match_sets_manual_time(session, tenant_cfg):
    tenant, _, m = await _no_time_match(session, tenant_cfg)
    text, buttons = await leader.time_command(session, tenant.id, _cmd(m))
    assert buttons == []
    assert text.startswith("Время записано: Соперник g1 — Westchester (АБЛ)")
    await session.refresh(m)
    assert m.start_at == _utc(m, 18, 30) and m.start_at.tzinfo is None
    assert (m.time_status, m.time_source) == (TimeStatus.TIME_SET, "manual")


async def test_time_variants_parse(session, tenant_cfg):
    """Точка вместо двоеточия, одна цифра в часе, текст вокруг."""
    tenant, _, m = await _no_time_match(session, tenant_cfg)
    text, _ = await leader.time_command(
        session, tenant.id, f" матча {m.match_date.day}.{m.match_date.month} 9.05"
    )
    assert text.startswith("Время записано")
    await session.refresh(m)
    assert m.start_at == _utc(m, 9, 5)


async def test_manual_time_can_be_corrected(session, tenant_cfg):
    tenant, _, m = await _no_time_match(session, tenant_cfg)
    await leader.time_command(session, tenant.id, _cmd(m, "18:30"))
    text, _ = await leader.time_command(session, tenant.id, _cmd(m, "19:00"))
    # смена уже заданного времени — новый опрос (ADR 0007)
    assert text.startswith("Время изменено:") and "19:00" in text
    assert "Опрос перезапускается: прежние отметки сброшены" in text
    await session.refresh(m)
    assert m.start_at == _utc(m, 19, 0) and m.time_source == "manual"


async def test_site_time_wins(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "g1", days=3)
    site = m.start_at
    text, buttons = await leader.time_command(session, tenant.id, _cmd(m))
    assert text.startswith("У матча уже есть время с сайта лиги:") and buttons == []
    await session.refresh(m)
    assert m.start_at == site and m.time_source is None


async def test_bad_format_and_not_found(session, tenant_cfg):
    tenant, _, m = await _no_time_match(session, tenant_cfg)
    for args in ("", " завтра в семь", " 25.10", " 18:30"):
        assert await leader.time_command(session, tenant.id, args) == (HINT, [])
    for args in (_cmd(m, "24:00"), _cmd(m, "18:60")):
        assert await leader.time_command(session, tenant.id, args) == (
            "Не понял время. Пример: «время 25.10 18:30».",
            [],
        )
    other = m.match_date + timedelta(days=1)
    assert await leader.time_command(session, tenant.id, f" {other:%d.%m} 18:30") == (
        f"Не нашёл нашего предстоящего матча на {other:%d.%m}.",
        [],
    )
    await session.refresh(m)
    assert m.start_at is None and m.time_source is None


async def test_past_match_not_found(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "old", days=-3, with_time=False)
    text, _ = await leader.time_command(session, tenant.id, _cmd(m))
    assert text.startswith("Не нашёл нашего предстоящего матча")


async def test_several_matches_buttons_then_tm(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    a = await _match(session, entries["abl"], "g1", days=3, with_time=False)
    b = await _match(session, entries["mlbl"], "g2", days=3, with_time=False)
    assert a.match_date == b.match_date
    text, rows = await leader.time_command(session, tenant.id, _cmd(a))
    assert text == "В этот день несколько матчей — для какого время 18:30?"
    assert sorted(btn.payload["m"] for [btn] in rows) == sorted([a.id, b.id])
    day = f"{a.match_date.day}.{a.match_date.month}"  # день из команды: для ожидаемых выходных — сб или вс
    assert all(btn.payload == {"a": "tm", "m": btn.payload["m"], "t": "18:30", "d": day} for [btn] in rows)
    assert all(isinstance(btn, Button) and len(btn.label) <= 40 for [btn] in rows)
    await session.refresh(a)
    assert a.start_at is None, "до выбора матча время не пишем"

    captain = await _player(session, tenant, CAPTAIN)
    ch = FakeChannel()
    press = ButtonPress(
        CAPTAIN, CAPTAIN, MessageRef(CAPTAIN, "500"), {"a": "tm", "m": b.id, "t": "18:30"}, "e"
    )
    text, buttons = await leader.on_button(session, ch, tenant.id, captain, press)
    assert text.startswith("Время записано") and buttons == []
    assert ch.toasts == ["Готово"]
    await session.refresh(a)
    await session.refresh(b)
    assert a.start_at is None and b.time_source == "manual" and b.start_at == _utc(b, 18, 30)


async def test_tm_foreign_or_missing_match(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    b, _, _ = await _second_tenant(session, entries)
    own_b = (
        await session.scalars(
            select(TeamEntry).where(
                TeamEntry.tenant_id == b.id, TeamEntry.league_team_id != entries["abl"].league_team_id
            )
        )
    ).one()
    foreign = await _match(session, own_b, "b1", days=3, with_time=False)
    captain = await _player(session, tenant, CAPTAIN)
    ch = FakeChannel()
    for mid in (foreign.id, 99999):
        press = ButtonPress(
            CAPTAIN, CAPTAIN, MessageRef(CAPTAIN, "500"), {"a": "tm", "m": mid, "t": "18:30"}, "e"
        )
        assert await leader.on_button(session, ch, tenant.id, captain, press) is None
    assert ch.toasts == ["Матч не найден", "Матч не найден"]
    await session.refresh(foreign)
    assert foreign.start_at is None and foreign.time_source is None
    # и текстом — чужой матч не находится
    text, _ = await leader.time_command(session, tenant.id, _cmd(foreign))
    assert text.startswith("Не нашёл нашего предстоящего матча")


async def test_tm_denied_for_player(session, tenant_cfg):
    tenant, _, m = await _no_time_match(session, tenant_cfg)
    ch = FakeChannel()
    press = ButtonPress(
        SIDOROV, SIDOROV, MessageRef(SIDOROV, "500"), {"a": "tm", "m": m.id, "t": "18:30"}, "e"
    )
    await handle(session, ch, press)
    assert ch.toasts == ["Это доступно только руководителям команды"]
    await session.refresh(m)
    assert m.start_at is None


async def test_dm_time_command_leader_only(session, tenant_cfg):
    tenant, _, m = await _no_time_match(session, tenant_cfg)
    ch = FakeChannel()
    await handle(
        session, ch, IncomingMessage(SIDOROV, SIDOROV, "время" + _cmd(m), is_chat=False, mentioned=False)
    )
    assert [t for p, t, _ in ch.sent if p == SIDOROV] == [UNKNOWN]
    await session.refresh(m)
    assert m.start_at is None

    await handle(
        session, ch, IncomingMessage(CAPTAIN, CAPTAIN, "Время" + _cmd(m), is_chat=False, mentioned=False)
    )
    [(peer, text, _)] = [x for x in ch.sent if x[0] == CAPTAIN]
    assert text.startswith("Время записано")
    await handle(session, ch, IncomingMessage(CAPTAIN, CAPTAIN, "время", is_chat=False, mentioned=False))
    assert ch.sent[-1][:2] == (CAPTAIN, HINT)


async def test_no_time_warning_hint_then_manual_time_starts_poll(session, tenant_cfg):
    """Предупреждение подсказывает «время ДД.ММ 18:30»; после ручного времени tick запускает опрос."""
    tenant, _, m = await _no_time_match(session, tenant_cfg, days=1)
    now = datetime.combine(m.match_date, time(10, 0), MSK)  # окно опроса (среда до игры) уже открыто
    ch = FakeChannel()
    await weekly.tick(session, ch, now=now)
    [warning] = [t for t, _ in _to(ch, CAPTAIN) if t.startswith(NO_TIME)]
    assert f"«время {m.match_date:%d.%m} 18:30»" in warning
    assert _to(ch, CHAT) == []

    await leader.time_command(session, tenant.id, _cmd(m, "18:30"))
    await weekly.tick(session, ch, now=now + timedelta(minutes=1))
    [(poll_text, buttons)] = _to(ch, CHAT)
    assert "Соперник g1 — Westchester" in poll_text
    assert [b.label for b in buttons[0]] == ["Буду", "Не буду"]
    assert (await session.scalars(select(PollMessage))).one().match_id == m.id
