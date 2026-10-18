"""M4: «состав» — состав на ближайший матч каждой лиги для руководителей (domain.md, «Состав на матч»).
Вымышленные имена."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from basket_assist.services import digests, polls, roster
from basket_assist.storage.tables import (
    Attendance,
    Chat,
    League,
    LeagueTeam,
    Match,
    MessengerAccount,
    Player,
    SeasonRosterEntry,
    utcnow,
)
from tests.fakes import FakeChannel
from tests.test_leader_bot import B_NAMES, FRIEND, PETROV, SIDOROV, _ready, _second_tenant
from tests.test_roster import CAPTAIN, VICE, _player

STAMP = re.compile(r"\n\nНа \d\d\.\d\d \d\d:\d\d$")


async def _match(session, entry, ext: str, days: float = 2, with_time: bool = True) -> Match:
    rival = LeagueTeam(
        league_id=(await session.get(LeagueTeam, entry.league_team_id)).league_id,
        external_id=f"rival-{ext}",
        name=f"Соперник {ext}",
    )
    session.add(rival)
    await session.flush()
    start = utcnow() + timedelta(days=days)
    m = Match(
        tournament_id=entry.tournament_id,
        external_id=ext,
        home_team_id=rival.id,  # мы — гости: «наши — соперник» не зависит от того, кто хозяин
        away_team_id=entry.league_team_id,
        match_date=start.date(),
        start_at=start if with_time else None,
        time_status="time_set" if with_time else "date_only",
        status="scheduled",
    )
    session.add(m)
    await session.commit()
    return m


async def _publish(session, m: Match) -> None:
    chat = (await session.execute(select(Chat))).scalar_one()
    await polls.publish(session, FakeChannel(), chat, m)


async def _vote(session, tenant, m: Match, player: Player, vote: str) -> None:
    session.add(Attendance(tenant_id=tenant.id, match_id=m.id, player_id=player.id, vote=vote))
    await session.commit()


async def _league(session, entry) -> League:
    return await session.get(League, (await session.get(LeagueTeam, entry.league_team_id)).league_id)


def _blocks(text: str) -> list[str]:
    """Блоки лиг без отметки «На …» (её формат проверяем, точное время — нет)."""
    assert STAMP.search(text), text
    return STAMP.sub("", text).split("\n\n🏀 ")


async def _lines(session, tenant, i: int = 0) -> list[str]:
    """Строки i-го блока (0 — АБЛ, 1 — МЛБЛ) без «🏀 » в начале второго и далее."""
    return _blocks(await digests.lineup_text(session, tenant.id))[i].split("\n")


# ---------- ветки блока ----------


async def test_no_matches_every_league_shown(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    text = await digests.lineup_text(session, tenant.id)
    assert _blocks(text) == [
        "🏀 АБЛ: Westchester\nБлижайших матчей на сайте лиги пока нет.",
        "МЛБЛ: МИРЭА\nБлижайших матчей на сайте лиги пока нет.",
    ]


async def test_no_time_and_no_poll(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "g1", with_time=False)
    await _publish(session, m)  # даже если анонс есть — без времени опрос не идёт
    await _match(session, entries["mlbl"], "g2")
    abl, mlbl = _blocks(await digests.lineup_text(session, tenant.id))
    assert abl.split("\n") == [
        "🏀 АБЛ: Westchester — Соперник g1",
        f"📅 {digests.format_when(m, await _league(session, entries['abl']))}",
        "Время не назначено — опрос не запускается, свяжитесь с лигой.",
    ]
    assert "время не назначено" in abl.split("\n")[1]
    assert mlbl.split("\n")[0] == "МЛБЛ: МИРЭА — Соперник g2"
    assert mlbl.split("\n")[2:] == ["Опрос ещё не запущен."]
    assert "Буду" not in abl + mlbl


async def test_nearest_match_per_entry(session, tenant_cfg):
    """Блок — по ближайшему матчу выступления, даже если анонс есть только у следующего."""
    tenant, entries = await _ready(session, tenant_cfg)
    later = await _match(session, entries["abl"], "late", days=5)
    await _publish(session, later)
    await _match(session, entries["abl"], "near", days=1)
    abl = await _lines(session, tenant)
    assert abl[0] == "🏀 АБЛ: Westchester — Соперник near"
    assert abl[2:] == ["Опрос ещё не запущен."]
    assert "Соперник late" not in await digests.lineup_text(session, tenant.id)


async def test_stamp_in_league_timezone(session, tenant_cfg, monkeypatch):
    tenant, _ = await _ready(session, tenant_cfg)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 18, 21, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(digests, "datetime", Frozen)
    text = await digests.lineup_text(session, tenant.id)
    assert text.endswith("\n\nНа 19.10 00:30")  # Europe/Moscow = UTC+3


async def test_no_entries(session):
    assert await digests.lineup_text(session, 999) == "Ближайших матчей на сайтах лиг пока нет."


# ---------- полный блок ----------


async def _full(session, tenant_cfg):
    """АБЛ (нужно 2–3): Буду — Капитанов №1, Петров (№7, правка 77), Ёлкин без номера;
    ⚠️ Заместитель (не сопоставлен), Друг Команды (нет в заявке), Denis Petrovich (не играет в лиге);
    Не буду — Сидоров."""
    tenant, entries = await _ready(session, tenant_cfg)
    abl = entries["abl"]
    league = await _league(session, abl)
    league.min_players, league.max_players = 2, 3
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, PETROV)
    petrovich = await _player(session, tenant, "12")
    friend = await _player(session, tenant, FRIEND)
    elkin = Player(tenant_id=tenant.id, display_name="Петя Ёлкин")
    row = SeasonRosterEntry(
        league_team_id=abl.league_team_id,
        tournament_id=abl.tournament_id,
        external_person_id="a9",
        last_name="Ёлкин",
        first_name="Пётр",
        number=None,
    )
    session.add_all([elkin, row])
    await session.flush()
    await roster.confirm(session, tenant.id, elkin.id, abl.id, row.id, captain.id)
    link = await roster._link(session, tenant.id, petrov.id, abl.id)
    link.number_override = "77"
    await roster.mark_not_in_league(session, tenant.id, petrovich.id, abl.id, captain.id)
    await roster.mark_not_in_roster(session, tenant.id, friend.id, abl.id, captain.id)
    await session.commit()
    m = await _match(session, abl, "g1")
    await _publish(session, m)
    for who in (captain, petrov, elkin, petrovich, friend, await _player(session, tenant, VICE)):
        await _vote(session, tenant, m, who, "going")
    await _vote(session, tenant, m, await _player(session, tenant, SIDOROV), "not_going")
    return tenant, entries, m


async def test_full_block(session, tenant_cfg):
    tenant, entries, m = await _full(session, tenant_cfg)
    assert await _lines(session, tenant) == [
        "🏀 АБЛ: Westchester — Соперник g1",
        f"📅 {digests.format_when(m, await _league(session, entries['abl']))}",
        "",
        "Буду (6):",
        "  № ? Ёлкин Пётр",  # «ё» сортируется как «е»; без номера — «№ ?»
        "  №1 Капитанов Иван",
        "  №77 Петров Александр",  # правка руководителя важнее №7 из заявки на сезон
        "  ⚠️ Denis Petrovich — не играет в этой лиге",
        "  ⚠️ Друг Команды — нет в заявке на сезон",
        "  ⚠️ Заместитель — не сопоставлен с заявкой на сезон",
        "В заявку на игру: 3 (нужно 2–3) ✅",
        "Не буду (1): Сидоров Евгений",
        "Не ответили (0): —",
    ]


async def test_number_from_season_roster_without_override(session, tenant_cfg):
    tenant, entries, _ = await _full(session, tenant_cfg)
    petrov = await _player(session, tenant, PETROV)
    (await roster._link(session, tenant.id, petrov.id, entries["abl"].id)).number_override = None
    await session.commit()
    assert "  №7 Петров Александр" in await _lines(session, tenant)


async def test_size_too_many_and_too_few(session, tenant_cfg):
    tenant, entries, m = await _full(session, tenant_cfg)
    sidorov = await _player(session, tenant, SIDOROV)
    att = (
        await session.execute(select(Attendance).filter_by(match_id=m.id, player_id=sidorov.id))
    ).scalar_one()
    att.vote = "going"
    await session.commit()
    lines = await _lines(session, tenant)
    assert "Буду (7):" in lines and "Не буду (0): —" in lines
    assert "В заявку на игру: 4 (нужно 2–3) — ⚠️ лишних 1" in lines

    league = await _league(session, entries["abl"])
    league.min_players, league.max_players = 5, 12
    await session.commit()
    assert "В заявку на игру: 4 (нужно 5–12) — ⚠️ не хватает 1" in await _lines(session, tenant)


@pytest.mark.parametrize(
    ("n", "tail"),
    [(0, " — ⚠️ не хватает 2"), (1, " — ⚠️ не хватает 1"), (2, " ✅"), (3, " ✅"), (4, " — ⚠️ лишних 1")],
)
def test_size_note_boundaries(n, tail):
    assert digests._size_note(n, 2, 3) == f"В заявку на игру: {n} (нужно 2–3){tail}"


async def test_size_counts_only_in_roster(session, tenant_cfg):
    """⚠️-игроки входят в «Буду (N)», но не в K: в заявку на игру идут только заявленные."""
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "g1")
    await _publish(session, m)
    for vk in (CAPTAIN, VICE, PETROV, SIDOROV, "12", FRIEND):
        await _vote(session, tenant, m, await _player(session, tenant, vk), "going")
    lines = await _lines(session, tenant)
    assert "Буду (6):" in lines
    assert "В заявку на игру: 3 (нужно 5–12) — ⚠️ не хватает 2" in lines


# ---------- «Не ответили» ----------


async def test_silent_only_active_in_roster_with_dm_mark(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    abl = entries["abl"]
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, PETROV)
    sidorov = await _player(session, tenant, SIDOROV)
    await roster.mark_not_in_league(session, tenant.id, petrov.id, abl.id, captain.id)  # был заявлен
    await roster.mark_not_team_player(session, tenant.id, sidorov.id)  # заявлен, но не игрок команды
    acc = (await session.execute(select(MessengerAccount).filter_by(player_id=captain.id))).scalar_one()
    acc.dm_allowed = False
    await session.commit()
    m = await _match(session, abl, "g1")
    await _publish(session, m)
    lines = await _lines(session, tenant)
    # Заместитель, Друг Команды, Denis Petrovich не сопоставлены в АБЛ — их не ждём
    assert lines[-1] == "Не ответили (1): Капитанов Иван 🔕"
    assert "Буду (0):" in lines and "Не буду (0): —" in lines

    # в МЛБЛ Петров заявлен и не ответил, личка открыта — без 🔕
    m2 = await _match(session, entries["mlbl"], "g2")
    await _publish(session, m2)
    assert (await _lines(session, tenant, 1))[-1] == "Не ответили (1): Петров Александр"


async def test_voters_not_in_silent(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "g1")
    await _publish(session, m)
    await _vote(session, tenant, m, await _player(session, tenant, PETROV), "not_going")
    lines = await _lines(session, tenant)
    assert "Не буду (1): Петров Александр" in lines
    assert lines[-1] == "Не ответили (2): Капитанов Иван, Сидоров Евгений"


# ---------- мультитенантность ----------


async def test_isolated_between_tenants(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _match(session, entries["abl"], "g1")
    await _publish(session, m)  # анонс — только у команды A
    b, gromov, _ = await _second_tenant(session, entries)  # у B выступление в той же команде АБЛ
    session.add(Attendance(tenant_id=b.id, match_id=m.id, player_id=gromov.id, vote="going"))
    await _vote(session, tenant, m, await _player(session, tenant, PETROV), "going")

    a_text = await digests.lineup_text(session, tenant.id)
    for name in (*B_NAMES, "Громов"):
        assert name not in a_text
    assert "Буду (1):" in a_text and "  №7 Петров Александр" in a_text

    b_text = await digests.lineup_text(session, b.id)
    b_blocks = _blocks(b_text)
    assert len(b_blocks) == 2
    assert b_blocks[0].split("\n")[2:] == ["Опрос ещё не запущен."], "анонс команды A — не анонс B"
    assert b_blocks[1] == "ВЛБЛ: Другие\nБлижайших матчей на сайте лиги пока нет."
    for name in ("Петров", "Сидоров", "Капитанов", "МЛБЛ", "МИРЭА"):
        assert name not in b_text
