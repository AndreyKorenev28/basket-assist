"""M3: сопоставление с заявками на сезон, права руководителя, сигналы (вымышленные имена)."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from basket_assist.bot.handlers import handle
from basket_assist.channels.base import ButtonPress, IncomingMessage, MessageRef
from basket_assist.services import leaders, polls, roster
from basket_assist.services.sync import _fill_numbers_from_protocols, team_ref
from basket_assist.sources.infobasket import parse_game_numbers
from basket_assist.storage.bootstrap import apply_tenant_config
from basket_assist.storage.tables import (
    League,
    LeagueTeam,
    Match,
    MessengerAccount,
    NotificationLog,
    Player,
    RosterLink,
    SeasonRosterEntry,
    TeamEntry,
    utcnow,
)
from tests.fakes import FakeChannel

CAPTAIN, VICE = "1", "2"  # VK ID из config/tenant.example.toml
CHAT = "2000000001"

ABL_ROSTER = [
    ("a1", "Петров", "Александр", "7"),
    ("a2", "Сидоров", "Евгений", "11"),
    ("a3", "Капитанов", "Иван", "1"),
]
MLBL_ROSTER = [("m1", "Петров", "Александр", None), ("m2", "Орлов", "Денис", None)]


async def _setup(session, tenant_cfg):
    tenant = await apply_tenant_config(session, tenant_cfg)
    entries = {}
    for key, rows in (("abl", ABL_ROSTER), ("mlbl", MLBL_ROSTER)):
        league = (await session.execute(select(League).filter_by(key=key))).scalar_one()
        entry = (
            await session.execute(
                select(TeamEntry)
                .join(LeagueTeam, LeagueTeam.id == TeamEntry.league_team_id)
                .where(LeagueTeam.league_id == league.id)
            )
        ).scalar_one()
        for pid, last, first, num in rows:
            session.add(
                SeasonRosterEntry(
                    league_team_id=entry.league_team_id,
                    tournament_id=entry.tournament_id,
                    external_person_id=pid,
                    last_name=last,
                    first_name=first,
                    number=num,
                )
            )
        entries[key] = entry
    await session.commit()
    return tenant, entries


async def _player(session, tenant, vk_id: str) -> Player:
    acc = (await session.execute(select(MessengerAccount).filter_by(external_user_id=vk_id))).scalar_one()
    return await session.get(Player, acc.player_id)


async def _entry_id(session, entry, person: str) -> int:
    return (
        await session.execute(
            select(SeasonRosterEntry.id).filter_by(
                league_team_id=entry.league_team_id, external_person_id=person
            )
        )
    ).scalar_one()


MEMBERS = [
    {"vk_id": 1, "first_name": "Иван", "last_name": "Капитанов"},
    {"vk_id": 10, "first_name": "Саша", "last_name": "Петров"},
    {"vk_id": 11, "first_name": "Evgeniy", "last_name": "Sidorov"},
    {"vk_id": 12, "first_name": "Denis", "last_name": "Petrovich"},
    {"vk_id": 13, "first_name": "Друг", "last_name": "Команды"},
    {"vk_id": 14, "first_name": "DELETED", "last_name": "", "deactivated": "deleted"},
]


async def test_import_queue_and_confirm_all(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    confirmed = [{"vk_id": 12, "last_name": "Орлов", "first_name": "Денис"}]
    stats = await roster.import_chat_members(session, tenant.id, MEMBERS, confirmed, confirmed_by_vk_id=2)
    assert stats["players"] == 4  # капитан уже был, удалённый аккаунт пропущен
    assert stats["confirmed"] == 1  # Орлов Денис — подтвердил руководитель, только в МЛБЛ
    # однозначные: Капитанов (АБЛ), Петров (АБЛ, МЛБЛ), Сидоров (АБЛ) — «предложено», не подтверждено
    assert stats["suggested"] == 4

    vice = await _player(session, tenant, VICE)
    n = await roster.confirm_all_unambiguous(session, tenant.id, vice.id)
    assert n == 4
    petrov = await _player(session, tenant, "10")
    assert await roster.roster_status(session, tenant.id, petrov.id, entries["abl"].id) == "in_roster"
    assert await roster.roster_status(session, tenant.id, petrov.id, entries["mlbl"].id) == "in_roster"
    link = (
        await session.execute(
            select(RosterLink).filter_by(player_id=petrov.id, team_entry_id=entries["abl"].id)
        )
    ).scalar_one()
    assert link.confirmed_by_player_id == vice.id


async def test_not_in_roster_per_league_and_not_team_player(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    await roster.import_chat_members(session, tenant.id, MEMBERS, [], None)
    vice = await _player(session, tenant, VICE)
    sidorov = await _player(session, tenant, "11")
    friend = await _player(session, tenant, "13")

    # Сидоров заявлен в АБЛ, но не в МЛБЛ — отмечаем «нет в заявке» только для МЛБЛ
    await roster.mark_not_in_roster(session, tenant.id, sidorov.id, entries["mlbl"].id, vice.id)
    assert await roster.roster_status(session, tenant.id, sidorov.id, entries["mlbl"].id) == "not_in_roster"
    assert await roster.roster_status(session, tenant.id, sidorov.id, entries["abl"].id) == "unmatched"

    await roster.mark_not_team_player(session, tenant.id, friend.id)
    await session.commit()
    q = await roster.queue(session, tenant.id)
    assert friend.id not in {i.player.id for i in q}
    assert (sidorov.id, entries["mlbl"].id) not in {(i.player.id, i.entry.id) for i in q}
    assert (sidorov.id, entries["abl"].id) in {(i.player.id, i.entry.id) for i in q}


async def test_conflict_and_cross_league_suggestion(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    await roster.import_chat_members(session, tenant.id, MEMBERS, [], None)
    vice = await _player(session, tenant, VICE)
    petrov = await _player(session, tenant, "10")
    sidorov = await _player(session, tenant, "11")
    a1 = await _entry_id(session, entries["abl"], "a1")
    await roster.confirm(session, tenant.id, petrov.id, entries["abl"].id, a1, vice.id)
    with pytest.raises(roster.MatchConflict):
        await roster.confirm(session, tenant.id, sidorov.id, entries["abl"].id, a1, vice.id)

    # «Basketball Fan» в VK, но уже связан в АБЛ как Петров Александр → в МЛБЛ предлагаем Петрова
    fan = Player(tenant_id=tenant.id, display_name="Basketball Fan")
    session.add(fan)
    await session.flush()
    a2 = await _entry_id(session, entries["abl"], "a2")
    await roster.confirm(session, tenant.id, fan.id, entries["abl"].id, a2, vice.id)
    cands = await roster.candidates(session, tenant.id, fan, entries["mlbl"])
    assert cands == []  # Сидорова в МЛБЛ нет
    pet_mlbl = await roster.candidates(session, tenant.id, petrov, entries["mlbl"])
    assert [c.key for c in pet_mlbl] == [await _entry_id(session, entries["mlbl"], "m1")]


async def test_acting_captain_rights(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    await roster.import_chat_members(session, tenant.id, MEMBERS, [], None)
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, "10")
    friend = await _player(session, tenant, "13")

    with pytest.raises(leaders.PermissionDenied):  # не сопоставлен — нельзя
        await leaders.grant_acting(session, tenant.id, captain.id, petrov.id, None)
    await roster.confirm_all_unambiguous(session, tenant.id, captain.id)
    with pytest.raises(leaders.PermissionDenied):  # выдаёт не капитан
        await leaders.grant_acting(session, tenant.id, friend.id, petrov.id, None)

    grant = await leaders.grant_acting(
        session, tenant.id, captain.id, petrov.id, utcnow() + timedelta(days=7), True
    )
    assert await leaders.is_leader(session, tenant.id, petrov.id)
    assert captain.id not in {
        p.id for p in await leaders.digest_recipients(session, tenant.id)
    }  # просил не присылать
    with pytest.raises(leaders.PermissionDenied):  # и. о. не может передавать дальше
        await leaders.grant_acting(session, tenant.id, petrov.id, captain.id, None)

    grant.valid_until = utcnow() - timedelta(minutes=1)  # срок истёк
    assert not await leaders.is_leader(session, tenant.id, petrov.id)
    grant.valid_until = None
    await leaders.revoke(session, tenant.id, grant.id, captain.id)
    assert not await leaders.is_leader(session, tenant.id, petrov.id)


def _press(user: str, payload: dict) -> ButtonPress:
    return ButtonPress(user, user, MessageRef(user, "500"), payload, "e")


async def test_leader_buttons_flow(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    await roster.import_chat_members(session, tenant.id, MEMBERS, [], None)
    ch = FakeChannel()
    sidorov = await _player(session, tenant, "11")

    # обычный игрок не может нажимать кнопки руководителя
    await handle(session, ch, _press("11", {"a": "lall"}))
    assert ch.toasts[-1] == "Это доступно только руководителям команды"

    await handle(session, ch, _press(VICE, {"a": "lall"}))
    assert ch.toasts[-1] == "Подтверждено: 4"
    await handle(session, ch, _press(VICE, {"a": "ln", "p": sidorov.id, "e": entries["mlbl"].id}))
    assert await roster.roster_status(session, tenant.id, sidorov.id, entries["mlbl"].id) == "not_in_roster"

    # «исправить»: найти и вернуть решение
    await handle(session, ch, IncomingMessage(VICE, VICE, "исправить сидор", is_chat=False, mentioned=False))
    assert "Кого исправить?" in ch.sent[-1][1]
    await handle(session, ch, _press(VICE, {"a": "lfix", "p": sidorov.id}))
    assert "нет в заявке на сезон" in ch.messages[(VICE, "500")]


async def test_vote_signal_and_roster_names(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    await roster.import_chat_members(session, tenant.id, MEMBERS, [], None)
    captain = await _player(session, tenant, CAPTAIN)
    await roster.confirm_all_unambiguous(session, tenant.id, captain.id)
    acc = (await session.execute(select(MessengerAccount).filter_by(external_user_id=CAPTAIN))).scalar_one()
    acc.dm_allowed = True
    entry = entries["abl"]
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
    chat_ref = MessageRef(CHAT, "101")
    from basket_assist.storage.tables import Chat

    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, ch, chat, m)
    # Петров сопоставлен → в анонсе ФИО из заявки; «Друг Команды» не сопоставлен → сигнал
    await handle(session, ch, ButtonPress(CHAT, "10", chat_ref, {"m": m.id, "v": "going"}, "e"))
    await handle(session, ch, ButtonPress(CHAT, "13", chat_ref, {"m": m.id, "v": "going"}, "e"))
    text = ch.messages[(CHAT, "101")]
    assert "Буду (2): Петров Александр, Друг Команды" in text
    alerts = [t for peer, t, _ in ch.sent if peer == CAPTAIN]
    assert len(alerts) == 1 and "Друг Команды" in alerts[0] and "не сопоставлен" in alerts[0]
    # повторное «Буду» после «Не буду» — сигнал не дублируется
    await handle(session, ch, ButtonPress(CHAT, "13", chat_ref, {"m": m.id, "v": "not_going"}, "e"))
    await handle(session, ch, ButtonPress(CHAT, "13", chat_ref, {"m": m.id, "v": "going"}, "e"))
    assert len([t for peer, t, _ in ch.sent if peer == CAPTAIN]) == 1
    assert len((await session.scalars(select(NotificationLog).filter_by(kind="not_in_roster"))).all()) == 1


async def test_numbers_from_protocols(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/infobasket/online_1090106.json").read_text(encoding="utf-8")
    )
    ours = parse_game_numbers(fixture, "9056")
    assert len(ours) == 12 and all(ours.values())

    entry = entries["mlbl"]
    team = await session.get(LeagueTeam, entry.league_team_id)
    session.add(
        Match(
            tournament_id=entry.tournament_id,
            external_id="1090106",
            home_team_id=team.id,
            away_team_id=team.id,
            match_date=utcnow().date(),
            time_status="time_set",
            status="played",
        )
    )
    await session.commit()

    class Adapter:
        roster_fields = frozenset({"position"})

        async def fetch_game_numbers(self, ref, game_ids):
            assert game_ids == ["1090106"]
            return {"m1": "21"}

    _, ref = await team_ref(session, entry)
    await _fill_numbers_from_protocols(session, entry, Adapter(), ref)
    row = (
        await session.execute(
            select(SeasonRosterEntry).filter_by(external_person_id="m1", league_team_id=team.id)
        )
    ).scalar_one()
    assert (row.number, row.number_source) == ("21", "protocol")


async def test_numbers_from_protocols_only_last_played_and_not_for_numbered_rosters(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    entry = entries["mlbl"]
    today = utcnow().date()
    for i in range(5):
        session.add(
            Match(
                tournament_id=entry.tournament_id,
                external_id=f"p{i}",
                home_team_id=entry.league_team_id,
                away_team_id=entry.league_team_id,
                match_date=today - timedelta(days=i),
                time_status="time_set",
                status="played" if i != 1 else "scheduled",
            )
        )
    await session.commit()
    calls = []

    class Adapter:
        roster_fields = frozenset()

        async def fetch_game_numbers(self, ref, game_ids):
            calls.append(game_ids)
            return {"m2": "33"}

    _, ref = await team_ref(session, entry)
    await _fill_numbers_from_protocols(session, entry, Adapter(), ref)
    assert calls == [["p0", "p2", "p3"]]  # 3 последних сыгранных, от новых к старым

    class Numbered(Adapter):
        roster_fields = frozenset({"number"})  # номера есть в заявке на сезон (АБЛ) — протоколы не читаем

    await _fill_numbers_from_protocols(session, entry, Numbered(), ref)
    assert len(calls) == 1
