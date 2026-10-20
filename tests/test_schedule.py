"""«Игры»: ближайшая игра в каждой лиге, скрытие сыгранных и начавшихся (schedule.upcoming_*)."""

from datetime import UTC, datetime, timedelta

import pytest

from basket_assist.bot.handlers import handle
from basket_assist.channels.base import IncomingMessage
from basket_assist.services import digests, schedule
from basket_assist.storage.tables import Match
from tests.fakes import FakeChannel
from tests.test_digests import _league, _match
from tests.test_leader_bot import SIDOROV, _ready
from tests.test_roster import CHAT

HOUR = 1 / 24


async def _shown(session, tenant) -> set[str]:
    return {m.external_id for *_, m in await schedule.upcoming_matches(session, tenant.id)}


# ---------- upcoming_matches: что скрывается ----------


async def test_started_hour_ago_still_shown(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    await _match(session, entries["abl"], "now", days=-HOUR)
    assert await _shown(session, tenant) == {"now"}


@pytest.mark.parametrize("minutes_ago", [120, 121, 180])
async def test_started_two_hours_ago_hidden(session, tenant_cfg, minutes_ago):
    tenant, entries = await _ready(session, tenant_cfg)
    await _match(session, entries["abl"], "old", days=-minutes_ago / 60 / 24)
    await _match(session, entries["abl"], "next", days=3)
    assert await _shown(session, tenant) == {"next"}


@pytest.mark.parametrize("status", ["played", "forfeit", "cancelled"])
async def test_finished_status_hidden_even_in_future(session, tenant_cfg, status):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "fin", days=2)
    await _match(session, entries["abl"], "next", days=5)
    m.status = status
    await session.commit()
    assert await _shown(session, tenant) == {"next"}


async def test_rescheduled_and_without_time_shown(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "res", days=2)
    m.status = "rescheduled"
    await session.commit()
    await _match(session, entries["mlbl"], "notime", days=1, with_time=False)
    assert await _shown(session, tenant) == {"res", "notime"}


async def test_lineup_text_uses_same_filter(session, tenant_cfg):
    """«Состав» тоже не показывает сыгранный/начавшийся 2 ч назад матч — берёт следующий."""
    tenant, entries = await _ready(session, tenant_cfg)
    played = await _match(session, entries["abl"], "played", days=1)
    played.status = "played"
    await session.commit()
    await _match(session, entries["abl"], "old", days=-3 * HOUR)
    await _match(session, entries["abl"], "next", days=4)
    text = await digests.lineup_text(session, tenant.id)
    assert "Соперник next" in text
    assert "Соперник played" not in text and "Соперник old" not in text


# ---------- format_when ----------


async def test_format_when(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    league = await _league(session, entries["abl"])  # Europe/Moscow
    m = Match(match_date=datetime(2026, 10, 25).date(), start_at=datetime(2026, 10, 25, 15, 30))
    assert schedule.format_when(m, league) == "вс 25.10, 18:30"
    m.start_at = None
    assert schedule.format_when(m, league) == "вс 25.10, время пока не назначено"


# ---------- upcoming_text ----------


async def test_upcoming_text_one_nearest_per_league(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    near = await _match(session, entries["abl"], "near", days=1)
    near.venue_name, near.venue_address = "Зал Тестовый", "ул. Примерная, 1"
    await _match(session, entries["abl"], "late", days=4)  # вторая игра той же лиги — не показываем
    m2 = await _match(session, entries["mlbl"], "m2", days=2, with_time=False)
    await session.commit()
    text = await schedule.upcoming_text(session, tenant.id)
    abl = await _league(session, entries["abl"])
    mlbl = await _league(session, entries["mlbl"])
    assert text == (
        "Ближайшие игры:\n\n"
        f"🏀 АБЛ: Соперник near — Westchester\n📅 {schedule.format_when(near, abl)}\n"
        "Зал Тестовый, ул. Примерная, 1\n🟣 Наша форма: тёмная\n"
        "Таблица лиги пока не опубликована\nМы vs они: ещё не играли\n\n"
        f"🏀 МЛБЛ: Соперник m2 — МИРЭА\n📅 {schedule.format_when(m2, mlbl)}\nзал пока не указан\n"
        "🟣 Наша форма: тёмная\nТаблица лиги пока не опубликована\nМы vs они: ещё не играли"
    )
    assert "Соперник late" not in text
    assert "время пока не назначено" in text


async def test_upcoming_text_league_without_matches_shown(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["mlbl"], "m2", days=2)
    m.venue_name = "Зал Тестовый"
    await session.commit()
    text = await schedule.upcoming_text(session, tenant.id)
    blocks = text.split("\n\n")
    assert blocks[0] == "Ближайшие игры:"
    assert blocks[1] == "🏀 АБЛ: Westchester\nБлижайших матчей на сайте лиги пока нет."
    assert blocks[2].startswith("🏀 МЛБЛ: Соперник m2 — МИРЭА\n📅 ") and blocks[2].endswith(
        "\nЗал Тестовый\n🟣 Наша форма: тёмная\nТаблица лиги пока не опубликована\nМы vs они: ещё не играли"
    )
    assert "Наша форма" not in blocks[1]  # у лиги без матчей подсказки о форме нет


async def test_upcoming_text_no_matches_anywhere(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    assert await schedule.upcoming_text(session, tenant.id) == (
        "Ближайшие игры:\n\n"
        "🏀 АБЛ: Westchester\nБлижайших матчей на сайте лиги пока нет.\n\n"
        "🏀 МЛБЛ: МИРЭА\nБлижайших матчей на сайте лиги пока нет."
    )


async def test_upcoming_text_no_entries(session):
    assert await schedule.upcoming_text(session, 999) == "Ближайших матчей на сайтах лиг пока нет."


async def test_upcoming_text_finished_falls_to_next(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    old = await _match(session, entries["abl"], "old", days=-3 * HOUR)
    await _match(session, entries["abl"], "next", days=3)
    text = await schedule.upcoming_text(session, tenant.id)
    assert "Соперник next" in text and "Соперник old" not in text
    assert old.match_date >= (datetime.now(UTC) - timedelta(days=1)).date()  # попал в выборку, но скрыт


async def test_games_command_in_chat_uses_upcoming_text(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    await _match(session, entries["abl"], "near", days=1)
    ch = FakeChannel()
    await handle(session, ch, IncomingMessage(CHAT, SIDOROV, "игры", is_chat=True, mentioned=True))
    [(peer, text, _)] = ch.sent
    assert (peer, text) == (CHAT, await schedule.upcoming_text(session, tenant.id))
