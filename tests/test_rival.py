"""Справка по сопернику в «Играх»: таблица лиги и очные встречи сезона (schedule.rival_lines).
Всё — с нашей стороны: наши очки первыми, независимо от того, кто хозяин. Вымышленные команды."""

from datetime import timedelta

import pytest

from basket_assist.services import polls, schedule
from basket_assist.storage.tables import LeagueTeam, Match, Standing, Tournament
from tests.test_digests import _match
from tests.test_leader_bot import _ready

NO_TABLE = "Таблица лиги пока не опубликована"
NO_GAMES = "Мы vs они: ещё не играли"


async def _setup(session, tenant_cfg):
    """Ближайший матч АБЛ (мы гости) — наша команда, соперник, сам матч."""
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "next", days=2)
    ours = await session.get(LeagueTeam, entries["abl"].league_team_id)
    rival = await session.get(LeagueTeam, m.home_team_id)
    return tenant, m, ours, rival


async def _played(session, m, home, away, sh, sa, days_ago, tournament_id=None, ext=None):
    """Прошедший матч между двумя командами со счётом (sh/sa могут быть None — счёта ещё нет)."""
    old = Match(
        tournament_id=tournament_id or m.tournament_id,
        external_id=ext or f"old-{days_ago}",
        home_team_id=home.id,
        away_team_id=away.id,
        match_date=m.match_date - timedelta(days=days_ago),
        start_at=m.start_at - timedelta(days=days_ago),
        time_status="time_set",
        status="played",
        score_home=sh,
        score_away=sa,
    )
    session.add(old)
    await session.commit()


def _standing(m, team, place, wins, losses, pf, pa) -> Standing:
    return Standing(
        tournament_id=m.tournament_id,
        league_team_id=team.id,
        place=place,
        wins=wins,
        losses=losses,
        points_for=pf,
        points_against=pa,
    )


# ---------- таблица лиги ----------


async def test_table_both_teams(session, tenant_cfg):
    _, m, ours, rival = await _setup(session, tenant_cfg)
    session.add_all([_standing(m, ours, 6, 3, 4, 500, 512), _standing(m, rival, 3, 5, 2, 600, 560)])
    await session.commit()
    lines = await schedule.rival_lines(session, m, ours, rival)
    assert lines == ["Мы: 6-е, 3–4 (−12). Они: 3-е, 5–2 (+40)", NO_GAMES]


async def test_table_zero_diff_no_points_no_place(session, tenant_cfg):
    _, m, ours, rival = await _setup(session, tenant_cfg)
    session.add_all([_standing(m, ours, 1, 2, 2, 300, 300), _standing(m, rival, None, 0, 0, None, None)])
    await session.commit()
    [table, _] = await schedule.rival_lines(session, m, ours, rival)
    assert table == "Мы: 1-е, 2–2 (±0). Они: 0–0"


@pytest.mark.parametrize("who", ["ours", "rival"])
async def test_table_missing_for_one_team(session, tenant_cfg, who):
    _, m, ours, rival = await _setup(session, tenant_cfg)
    session.add(_standing(m, ours if who == "ours" else rival, 2, 4, 1, 400, 350))
    await session.commit()
    [table, _] = await schedule.rival_lines(session, m, ours, rival)
    assert table == NO_TABLE


async def test_table_of_other_tournament_ignored(session, tenant_cfg):
    _, m, ours, rival = await _setup(session, tenant_cfg)
    other = Tournament(league_id=ours.league_id, external_id="other-season", name="Прошлый сезон")
    session.add(other)
    await session.flush()
    for team in (ours, rival):
        st = _standing(m, team, 1, 1, 0, 80, 70)
        st.tournament_id = other.id
        session.add(st)
    await session.commit()
    [table, _] = await schedule.rival_lines(session, m, ours, rival)
    assert table == NO_TABLE


# ---------- очные встречи ----------


async def test_head_to_head_our_points_first_regardless_of_host(session, tenant_cfg):
    _, m, ours, rival = await _setup(session, tenant_cfg)
    await _played(session, m, rival, ours, 65, 78, days_ago=30)  # мы гости, выиграли 78:65
    await _played(session, m, ours, rival, 60, 71, days_ago=14)  # мы хозяева, проиграли 60:71
    [_, h2h] = await schedule.rival_lines(session, m, ours, rival)
    assert h2h == "Мы vs они: 1–1 (78:65, 60:71)"


async def test_head_to_head_ordered_by_date(session, tenant_cfg):
    _, m, ours, rival = await _setup(session, tenant_cfg)
    await _played(session, m, ours, rival, 90, 80, days_ago=7)
    await _played(session, m, ours, rival, 70, 75, days_ago=40)
    await _played(session, m, rival, ours, 50, 55, days_ago=20)
    [_, h2h] = await schedule.rival_lines(session, m, ours, rival)
    assert h2h == "Мы vs они: 2–1 (70:75, 55:50, 90:80)"


async def test_head_to_head_draw_not_counted(session, tenant_cfg):
    _, m, ours, rival = await _setup(session, tenant_cfg)
    await _played(session, m, ours, rival, 70, 70, days_ago=10)
    [_, h2h] = await schedule.rival_lines(session, m, ours, rival)
    assert h2h == "Мы vs они: 0–0 (70:70)"


async def test_head_to_head_ignores_unscored_other_tournament_and_other_teams(session, tenant_cfg):
    _, m, ours, rival = await _setup(session, tenant_cfg)
    await _played(session, m, ours, rival, None, None, days_ago=5, ext="noscore")
    await _played(session, m, ours, rival, 80, None, days_ago=6, ext="half")
    other = Tournament(league_id=ours.league_id, external_id="cup", name="Кубок")
    third = LeagueTeam(league_id=ours.league_id, external_id="third", name="Третья команда")
    session.add_all([other, third])
    await session.flush()
    await _played(session, m, ours, rival, 99, 10, days_ago=8, tournament_id=other.id, ext="cup")
    await _played(session, m, ours, third, 88, 11, days_ago=9, ext="third")
    await _played(session, m, rival, third, 77, 12, days_ago=11, ext="rival-third")
    assert await schedule.rival_lines(session, m, ours, rival) == [NO_TABLE, NO_GAMES]


# ---------- где показывается ----------


async def test_upcoming_text_contains_rival_lines(session, tenant_cfg):
    tenant, m, ours, rival = await _setup(session, tenant_cfg)
    session.add_all([_standing(m, ours, 2, 5, 1, 480, 400), _standing(m, rival, 4, 3, 3, 420, 430)])
    await session.commit()
    await _played(session, m, rival, ours, 61, 72, days_ago=21)
    text = await schedule.upcoming_text(session, tenant.id)
    abl = text.split("\n\n")[1].split("\n")
    assert abl[2:] == [
        "зал пока не указан",
        "🟣 Наша форма: тёмная",
        "Мы: 2-е, 5–1 (+80). Они: 4-е, 3–3 (−10)",
        "Мы vs они: 1–0 (72:61)",
    ]


async def test_poll_has_rival_lines(session, tenant_cfg):
    """ADR 0008: в анонсе та же справка, что в «Играх» — после цвета формы, до «Кто идёт?»."""
    tenant, m, ours, rival = await _setup(session, tenant_cfg)
    session.add_all([_standing(m, ours, 2, 5, 1, 480, 400), _standing(m, rival, 4, 3, 3, 420, 430)])
    await session.commit()
    await _played(session, m, rival, ours, 61, 72, days_ago=21)
    lines = (await polls.render(session, m, tenant.id)).split("\n")
    expected = await schedule.rival_lines(session, m, ours, rival)
    assert expected and expected[0].startswith("Мы:") and expected[-1].startswith("Мы vs они")
    i = lines.index(expected[0])
    assert "Наша форма" in lines[i - 1]
    assert lines[i : i + len(expected)] == expected
    assert lines[i + len(expected)] == "" and NO_TABLE not in lines


async def test_poll_rival_lines_without_table(session, tenant_cfg):
    tenant, m, _, _ = await _setup(session, tenant_cfg)
    text = await polls.render(session, m, tenant.id)
    assert NO_TABLE in text and NO_GAMES in text
