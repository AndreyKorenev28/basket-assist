"""Контрактные тесты адаптеров на реальных (обезличенных) ответах API — tests/fixtures/."""

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from basket_assist.sources import infobasket, mtgame
from basket_assist.sources.base import SourceError, TeamRef

FIX = Path(__file__).parent / "fixtures"


def load(rel: str):
    return json.loads((FIX / rel).read_text(encoding="utf-8"))


ABL = TeamRef("https://ablforpeople.com", "279", "4145", "231")
MLBL = TeamRef("https://moscow.ilovebasket.ru", "9056", "150011", "150000")


# ---------- MTGame (АБЛ) ----------


def test_mtgame_games():
    games = mtgame.parse_games(load("mtgame/games_team.json"))
    assert len(games) == 7
    g = next(x for x in games if x.external_id == "163719")
    assert g.start_at == datetime(2026, 10, 17, 17, 40)  # UTC
    assert g.match_date == date(2026, 10, 17)
    assert (
        g.home.external_id == "279" and g.home.name == "Westchester"
    )  # имя из tournament_team, не «Whestchester»
    assert (g.played, g.score_home, g.score_away) == (True, 57, 76)
    assert g.venue_address == "Россия, Москва, 2-я Магистральная улица, 16А"


def test_mtgame_standings_and_roster():
    st = mtgame.parse_standings(load("mtgame/tournament_teams.json"))
    assert len(st) == 32 and st[0].place == 1
    ours = next(s for s in st if s.team.external_id == "279")
    assert ours.wins + ours.losses == 7

    roster = mtgame.parse_roster(load("mtgame/roster.json"), with_birth_date=False)
    assert len(roster) == 19
    assert all(r.number for r in roster)  # в АБЛ номера есть в заявке на сезон
    assert all(r.birth_date is None for r in roster)  # дату рождения не храним, пока не включено
    assert mtgame.parse_roster(load("mtgame/roster.json"), with_birth_date=True)[0].birth_date == date(
        2000, 1, 1
    )


@respx.mock
async def test_mtgame_adapter_resolves_tournament_team_for_roster():
    respx.get("https://mtgame.ru/api/v1/tournament/4145/teams/").respond(
        json=load("mtgame/tournament_teams.json")
    )
    roster_route = respx.get("https://mtgame.ru/api/v1/tournament_team/31988/users/").respond(
        json=load("mtgame/roster.json")
    )
    async with httpx.AsyncClient() as client:
        roster = await mtgame.MtgameAdapter(client).fetch_roster(ABL)
    assert roster_route.called and len(roster) == 19


@respx.mock
async def test_mtgame_http_error_is_loud():
    respx.get(url__startswith="https://mtgame.ru/api/v1/tournament/4145/games/").respond(500)
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="HTTP 500"):
            await mtgame.MtgameAdapter(client).fetch_matches(ABL)


# ---------- Infobasket (МЛБЛ) ----------


def test_infobasket_games():
    games = infobasket.parse_games(load("infobasket/team_games.json"))
    assert len(games) == 10
    g = next(x for x in games if x.external_id == "1095970")
    assert g.start_at == datetime(2026, 10, 18, 14, 0)  # 17:00 МСК
    assert g.away.external_id == "9056" and g.away.name == "МИРЭА"
    assert g.venue_name == "ТМ. Арена" and g.venue_address is None
    assert all(x.start_at for x in games)  # в МЛБЛ время известно заранее


def test_infobasket_has_time_false_means_no_time():
    item = dict(load("infobasket/team_games.json")[0], HasTime=False)
    assert infobasket.parse_games([item])[0].start_at is None


def test_infobasket_roster_and_standings():
    roster = infobasket.parse_roster(load("infobasket/roster.json"), with_birth_date=False)
    assert len(roster) == 16
    assert all(r.number is None for r in roster)  # номеров в заявке на сезон МЛБЛ нет
    assert all(r.position for r in roster)

    st = infobasket.parse_standings(load("infobasket/standings.json"))
    assert len(st) == 12 and [s.place for s in st] == list(range(1, 13))
    assert any(s.team.external_id == "9056" for s in st)


def test_infobasket_ms_date():
    assert infobasket.parse_ms_date("/Date(1792332000000)/") == datetime(2026, 10, 18, 14, 0)
    assert infobasket.parse_ms_date("") is None


@respx.mock
async def test_infobasket_adapter_handles_bom():
    body = "﻿" + json.dumps(load("infobasket/team_games.json"), ensure_ascii=False)
    respx.get(url__startswith="https://reg.infobasket.su/Widget/TeamGames/9056").respond(
        content=body.encode("utf-8")
    )
    async with httpx.AsyncClient() as client:
        games = await infobasket.InfobasketAdapter(client).fetch_matches(MLBL)
    assert len(games) == 10


def test_infobasket_garbage_is_loud():
    with pytest.raises(SourceError):
        infobasket.parse_roster({"TeamID": 1}, with_birth_date=False)


def _protocol(team_id: str, players: list[dict]) -> dict:
    return {
        "GameTeams": [
            {"TeamID": 1, "Players": [{"PersonID": 5, "DisplayNumber": "99"}]},
            {"TeamID": int(team_id), "Players": players},
        ]
    }


def test_infobasket_game_numbers_fallbacks():
    data = _protocol(
        "9056",
        [
            {"PersonID": 1, "DisplayNumber": "7"},
            {"PersonID": 2, "PlayerNumber": 23},  # нет DisplayNumber — берём PlayerNumber
            {"PersonID": 3},  # без номера — пропускаем
            {"DisplayNumber": "4"},  # без PersonID — пропускаем
        ],
    )
    assert infobasket.parse_game_numbers(data, "9056") == {"1": "7", "2": "23"}
    assert infobasket.parse_game_numbers(data, "1234") == {}  # нашей команды в протоколе нет
    assert infobasket.parse_game_numbers({}, "9056") == {}


@respx.mock
async def test_infobasket_fetch_game_numbers_newest_wins():
    respx.get(url__startswith="https://reg.infobasket.su/Widget/GetOnline/2").respond(
        json=_protocol("9056", [{"PersonID": 1, "DisplayNumber": "8"}])
    )
    respx.get(url__startswith="https://reg.infobasket.su/Widget/GetOnline/1").respond(
        json=_protocol("9056", [{"PersonID": 1, "DisplayNumber": "7"}, {"PersonID": 2, "DisplayNumber": "5"}])
    )
    async with httpx.AsyncClient() as client:
        numbers = await infobasket.InfobasketAdapter(client).fetch_game_numbers(MLBL, ["2", "1"])
    assert numbers == {"1": "8", "2": "5"}  # игры — от новых к старым
