"""Хозяин слева, гость справа, и цвет формы (domain.md, «Хозяин и гость», «Цвет формы»).
Проверяем все места показа матча: «игры», «состав», анонс, сигнал «нет в заявке на сезон». Вымышленные имена."""

import pytest
from sqlalchemy import select

from basket_assist.bot.handlers import handle
from basket_assist.channels.base import ButtonPress, MessageRef
from basket_assist.services import digests, polls, roster, schedule
from basket_assist.storage.tables import Chat, League, LeagueTeam, Tournament
from tests.fakes import FakeChannel
from tests.test_digests import _blocks, _match
from tests.test_leader_bot import PETROV, _dms, _ready
from tests.test_roster import CAPTAIN, CHAT, VICE, _player

# (home, заголовок матча АБЛ, подсказка о форме)
CASES = [
    (True, "Westchester — Соперник g1", "👕 Наша форма: светлая"),
    (False, "Соперник g1 — Westchester", "👕 Наша форма: тёмная"),
]
IDS = ["мы хозяева", "мы гости"]


@pytest.mark.parametrize(("home", "title", "kit"), CASES, ids=IDS)
async def test_teams_and_kit_hint(session, tenant_cfg, home, title, kit):
    _, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "g1", home=home)
    ours = await session.get(LeagueTeam, entries["abl"].league_team_id)
    rival_id = m.away_team_id if home else m.home_team_id
    rival = await session.get(LeagueTeam, rival_id)
    assert schedule.teams(m, ours, rival) == title
    assert schedule.kit_hint(m, ours) == kit


@pytest.mark.parametrize(("home", "title", "kit"), CASES, ids=IDS)
async def test_upcoming_text_order_and_kit(session, tenant_cfg, home, title, kit):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "g1", home=home)
    m.venue_name = "Зал Тестовый"
    await session.commit()
    blocks = (await schedule.upcoming_text(session, tenant.id)).split("\n\n")
    assert blocks[1].split("\n") == [
        f"🏀 АБЛ: {title}",
        f"📅 {schedule.format_when(m, await _league_of(session, m))}",
        "📍 Зал Тестовый",
        kit,
    ]
    # МЛБЛ без матчей — без подсказки о форме
    assert blocks[2] == "🏀 МЛБЛ: МИРЭА\nБлижайших матчей на сайте лиги пока нет."


@pytest.mark.parametrize(("home", "title", "kit"), CASES, ids=IDS)
async def test_lineup_order_and_kit(session, tenant_cfg, home, title, kit):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "g1", home=home)
    abl, mlbl = _blocks(await digests.lineup_text(session, tenant.id))
    assert abl.split("\n")[:3] == [
        f"🏀 АБЛ: {title}",
        f"📅 {digests.format_when(m, await _league_of(session, m))}",
        kit,
    ]
    assert mlbl == "МЛБЛ: МИРЭА\nБлижайших матчей на сайте лиги пока нет."
    assert "👕" not in mlbl


@pytest.mark.parametrize(("home", "title", "kit"), CASES, ids=IDS)
async def test_poll_render_order_and_kit(session, tenant_cfg, home, title, kit):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "g1", home=home)
    lines = (await polls.render(session, m, tenant.id)).split("\n")
    assert lines[0] == f"🏀 АБЛ: {title}"
    assert lines[2].startswith("📍 ") and lines[3] == kit
    assert lines[4] == ""


@pytest.mark.parametrize(("home", "title", "kit"), CASES, ids=IDS)
async def test_not_in_roster_signal_order(session, tenant_cfg, home, title, kit):
    tenant, entries = await _ready(session, tenant_cfg)
    petrov = await _player(session, tenant, PETROV)
    captain = await _player(session, tenant, CAPTAIN)
    await roster.mark_not_in_roster(session, tenant.id, petrov.id, entries["abl"].id, captain.id)
    m = await _match(session, entries["abl"], "g1", home=home)
    ch = FakeChannel()
    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, ch, chat, m)
    await handle(
        session, ch, ButtonPress(CHAT, PETROV, MessageRef(CHAT, "101"), {"m": m.id, "v": "going"}, "e")
    )
    [mine] = [t for t in _dms(ch, PETROV) if "нет в заявке на сезон" in t]
    assert f"на матч {title} (АБЛ)," in mine
    for lead in (CAPTAIN, VICE):
        [alert] = [t for t in _dms(ch, lead) if "нет в заявке на сезон" in t]
        assert f"«Буду» на {title} (АБЛ)," in alert


async def _league_of(session, m):
    return await session.get(League, (await session.get(Tournament, m.tournament_id)).league_id)
