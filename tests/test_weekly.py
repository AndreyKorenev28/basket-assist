"""M4: недельный цикл матча — запуск опроса в среду, «время не назначено», сводки за 24 ч и 2 ч,
изменения состава после сводки (domain.md, «Жизненный цикл матча»; architecture.md §5). Вымышленные имена."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select

from basket_assist.bot import leader
from basket_assist.bot.handlers import handle
from basket_assist.channels.base import Button, ButtonPress, ChannelError, MessageRef
from basket_assist.services import digests, leaders, notify, weekly
from basket_assist.storage.tables import (
    Attendance,
    Chat,
    League,
    LeagueTeam,
    Match,
    MessengerAccount,
    NotificationLog,
    PollMessage,
    TeamEntry,
    utcnow,
)
from tests.fakes import FakeChannel
from tests.test_digests import _full, _publish
from tests.test_digests import _match as _rel_match
from tests.test_leader_bot import PETROV, SIDOROV, _ready, _second_tenant
from tests.test_roster import CAPTAIN, CHAT, VICE, _player

MSK = ZoneInfo("Europe/Moscow")
WED, THU, FRI, SAT, SUN = (date(2026, 10, d) for d in (21, 22, 23, 24, 25))
NO_TIME = "⏰ Время матча не назначено"
D24 = "📋 Сводка за сутки до игры"
D2 = "📋 Финальная сводка: игра через 2 часа"


def at(day: date, hh: int, mm: int = 0, tz: ZoneInfo = MSK) -> datetime:
    """Момент по времени лиги (aware) — так передаём now в tick."""
    return datetime.combine(day, time(hh, mm), tz)


def naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


async def _week_match(session, entry, ext: str, day: date = SAT, hhmm: tuple[int, int] | None = (12, 0)):
    """Наш матч в день `day`; hhmm — время начала по Москве, None — время не назначено."""
    rival = LeagueTeam(
        league_id=(await session.get(LeagueTeam, entry.league_team_id)).league_id,
        external_id=f"rival-{ext}",
        name=f"Соперник {ext}",
    )
    session.add(rival)
    await session.flush()
    m = Match(
        tournament_id=entry.tournament_id,
        external_id=ext,
        home_team_id=rival.id,
        away_team_id=entry.league_team_id,
        match_date=day,
        start_at=naive_utc(at(day, *hhmm)) if hhmm else None,
        time_status="time_set" if hhmm else "date_only",
        status="scheduled",
    )
    session.add(m)
    await session.commit()
    return m


REMIND = "⏰ Напоминание"


def _to(ch: FakeChannel, peer: str, reminders: bool = False) -> list[tuple[str, list | None]]:
    """Сообщения в peer; напоминания молчащим (за 30 ч) — только если reminders=True."""
    return [(t, b) for p, t, b in ch.sent if p == peer and (reminders or not t.startswith(REMIND))]


def _texts(ch: FakeChannel, peer: str, prefix: str = "") -> list[str]:
    return [t for t, _ in _to(ch, peer) if t.startswith(prefix)]


async def _league(session, entry) -> League:
    return await session.get(League, (await session.get(LeagueTeam, entry.league_team_id)).league_id)


async def _vote(session, tenant_id: int, m: Match, player, vote: str = "going") -> None:
    session.add(Attendance(tenant_id=tenant_id, match_id=m.id, player_id=player.id, vote=vote))
    await session.commit()


# ---------- poll_opens_at ----------


def _opens(day: date, weekday: int = 2, hhmm: str = "10:00", tz: str = "Europe/Moscow") -> datetime:
    return weekly.poll_opens_at(
        Match(match_date=day), League(timezone=tz, poll_weekday=weekday, poll_time=hhmm)
    )


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (SAT, datetime(2026, 10, 21, 7, 0)),  # 10:00 МСК = 07:00 UTC
        (SUN, datetime(2026, 10, 21, 7, 0)),
        (WED, datetime(2026, 10, 21, 7, 0)),  # игра в среду — та же среда
        (THU, datetime(2026, 10, 21, 7, 0)),
        (date(2026, 10, 26), datetime(2026, 10, 21, 7, 0)),  # пн — среда прошлой недели
        (date(2026, 10, 27), datetime(2026, 10, 21, 7, 0)),  # вт — тоже
        (date(2026, 10, 28), datetime(2026, 10, 28, 7, 0)),  # следующая среда
    ],
)
def test_poll_opens_default_wednesday(day, expected):
    assert _opens(day) == expected
    assert _opens(day).tzinfo is None


@pytest.mark.parametrize(
    ("day", "weekday", "hhmm", "expected"),
    [
        (SAT, 4, "18:30", datetime(2026, 10, 23, 15, 30)),  # пятница 18:30 МСК
        (FRI, 4, "18:30", datetime(2026, 10, 23, 15, 30)),
        (THU, 4, "18:30", datetime(2026, 10, 16, 15, 30)),  # пятница прошлой недели
        (SUN, 0, "09:05", datetime(2026, 10, 19, 6, 5)),
        (SUN, 6, "00:00", datetime(2026, 10, 24, 21, 0)),  # полночь МСК — накануне по UTC
    ],
)
def test_poll_opens_custom_weekday_and_time(day, weekday, hhmm, expected):
    assert _opens(day, weekday, hhmm) == expected


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        ("Europe/Kaliningrad", datetime(2026, 10, 21, 8, 0)),
        ("Asia/Yekaterinburg", datetime(2026, 10, 21, 5, 0)),
        ("Asia/Vladivostok", datetime(2026, 10, 21, 0, 0)),  # UTC+10
        ("UTC", datetime(2026, 10, 21, 10, 0)),
    ],
)
def test_poll_opens_in_league_timezone(tz, expected):
    assert _opens(SAT, tz=tz) == expected


# ---------- запуск опроса ----------


async def test_poll_starts_wednesday_10_once(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    for now in (at(date(2026, 10, 19), 12), at(WED, 9, 59)):  # МЛБЛ-случай: время известно заранее
        await weekly.tick(session, ch, now=now)
    assert ch.sent == [] and ch.pins == []

    await weekly.tick(session, ch, now=at(WED, 10))
    [(text, buttons)] = _to(ch, CHAT)
    assert "Соперник g1 — Westchester" in text
    assert [b.label for b in buttons[0]] == ["Буду", "Не буду"]
    assert ch.pins == [MessageRef(CHAT, "101")]
    assert ch.sent == [(CHAT, text, buttons)], "руководителям ничего не пишем"

    for now in (at(WED, 10, 1), at(WED, 11, 30), at(THU, 10)):
        await weekly.tick(session, ch, now=now)
    assert len(ch.sent) == 1 and len(ch.pins) == 1
    poll = (await session.scalars(select(PollMessage))).one()
    assert poll.tenant_id == tenant.id and poll.match_id == m.id


async def test_poll_respects_league_timezone(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    league = await _league(session, entries["abl"])
    league.timezone = "Asia/Vladivostok"
    await session.commit()
    await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    await weekly.tick(session, ch, now=datetime(2026, 10, 20, 23, 59, tzinfo=UTC))
    assert ch.sent == []
    await weekly.tick(session, ch, now=datetime(2026, 10, 21, 0, 0, tzinfo=UTC))  # ср 10:00 Владивосток
    assert len(_to(ch, CHAT)) == 1


async def test_time_appears_on_friday_poll_at_next_tick(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1", hhmm=None)
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    await weekly.tick(session, ch, now=at(THU, 10))
    assert _to(ch, CHAT) == [], "без времени опрос не запускается"

    m.start_at, m.time_status = naive_utc(at(SAT, 12)), "time_set"
    await session.commit()
    await weekly.tick(session, ch, now=at(FRI, 15, 17))
    assert len(_to(ch, CHAT)) == 1 and len(ch.pins) == 1


class FailingChat(FakeChannel):
    """VK не принимает сообщение в беседу (сбой), личка работает."""

    def __init__(self):
        super().__init__()
        self.chat_attempts = 0

    async def send(self, peer_id, text, buttons=None):
        if peer_id == CHAT:
            self.chat_attempts += 1
            raise ChannelError(None, "сбой VK")
        return await super().send(peer_id, text, buttons)


async def test_poll_publish_error_retried_hourly(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    await _week_match(session, entries["abl"], "g1")
    ch = FailingChat()
    for now in (at(WED, 10), at(WED, 10, 1), at(WED, 10, 59)):
        await weekly.tick(session, ch, now=now)
    assert ch.chat_attempts == 1, "не чаще раза в час"
    await weekly.tick(session, ch, now=at(WED, 11))
    assert ch.chat_attempts == 2
    assert (await session.scalars(select(PollMessage))).all() == []


async def test_no_chat_is_not_an_error(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    await _week_match(session, entries["abl"], "g1")
    await session.execute(delete(Chat))
    await session.commit()
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    assert ch.sent == []


# ---------- время не назначено ----------


async def test_no_time_warning_daily(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    await _week_match(session, entries["abl"], "g1", hhmm=None)
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 9, 59))
    assert ch.sent == [], "до среды 10:00 — тишина"

    await weekly.tick(session, ch, now=at(WED, 10))
    for peer in (CAPTAIN, VICE):
        [text] = _texts(ch, peer)
        assert text.startswith(NO_TIME) and "Соперник g1 — Westchester (АБЛ)" in text
        assert "Свяжитесь с лигой" in text
    assert _texts(ch, PETROV) == [] and _to(ch, CHAT) == []

    for now in (at(WED, 10, 1), at(WED, 23, 59)):
        await weekly.tick(session, ch, now=now)
    assert len(_texts(ch, CAPTAIN)) == 1, "второе предупреждение в тот же день не уходит"

    for now in (at(THU, 0, 0), at(THU, 9, 59)):  # новый день, но ещё не время опроса — не будим ночью
        await weekly.tick(session, ch, now=now)
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 1
    await weekly.tick(session, ch, now=at(THU, 10))  # повтор — во время опроса лиги
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 2 and len(_texts(ch, VICE, NO_TIME)) == 2
    await weekly.tick(session, ch, now=at(THU, 23, 59))
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 2
    for now in (at(FRI, 9), at(FRI, 10), at(SAT, 9, 59)):
        await weekly.tick(session, ch, now=now)
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 3
    await weekly.tick(session, ch, now=at(SAT, 10))  # день матча — тоже с 10:00
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 4
    await weekly.tick(session, ch, now=at(SUN, 10))  # день матча прошёл
    assert len(_texts(ch, CAPTAIN, NO_TIME)) == 4
    assert _to(ch, CHAT) == [] and ch.pins == []


async def test_no_time_recipient_without_dm_and_muted_grantor(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    vice = await _player(session, tenant, VICE)
    acc = (await session.execute(select(MessengerAccount).filter_by(player_id=vice.id))).scalar_one()
    acc.dm_allowed = False
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, PETROV)
    await leaders.grant_acting(
        session, tenant.id, captain.id, petrov.id, utcnow() + timedelta(days=30), suppress_digests=True
    )
    await session.commit()
    await _week_match(session, entries["abl"], "g1", hhmm=None)
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    assert _texts(ch, CAPTAIN) == [], "капитан попросил не присылать на время передачи прав"
    assert _texts(ch, VICE) == [], "личка закрыта — просто не доставлено"
    assert [t[: len(NO_TIME)] for t in _texts(ch, PETROV)] == [NO_TIME]


# ---------- сводки за 24 ч и за 2 ч ----------

START = at(SAT, 12)


def _dg_buttons(m: Match, kind: str) -> list[list[Button]]:
    ref = {"m": m.id, "k": kind}
    return [
        [Button("➕ Добавить", {"a": "aa", **ref}), Button("➖ Убрать", {"a": "ar", **ref})],
        [
            Button("🔄 Обновить", {"a": "dg", **ref}, "secondary"),
            Button(weekly.COPY_BUTTON, {"a": "ac", "m": m.id}, "secondary"),
        ],
    ]


async def test_digest24_and_digest2_once_each(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    await _vote(session, tenant.id, m, await _player(session, tenant, PETROV))
    ch = FakeChannel()
    await weekly.tick(session, ch, now=START - timedelta(hours=24, minutes=1))
    assert _to(ch, CAPTAIN) == []

    await weekly.tick(session, ch, now=START - timedelta(hours=24))
    for peer in (CAPTAIN, VICE):
        [(digest, buttons), (application, app_buttons)] = _to(ch, peer)
        assert digest.startswith(f"{D24}\n\n🏀 АБЛ: Соперник g1 — Westchester")
        assert "  №7 Петров Александр" in digest
        assert digest.endswith(f"\n\n{weekly.NUMBER_HINT}\n\n{weekly.APPLICATION_HINT}")
        assert buttons == _dg_buttons(m, "digest24")
        assert application == "7 Петров Александр" and app_buttons is None
    assert _to(ch, PETROV) == []

    for now in (START - timedelta(hours=23), START - timedelta(hours=2, minutes=1)):
        await weekly.tick(session, ch, now=now)
    assert len(_to(ch, CAPTAIN)) == 2

    await weekly.tick(session, ch, now=START - timedelta(hours=2))
    digest, buttons = _to(ch, CAPTAIN)[2]
    assert digest.startswith(f"{D2}\n\n") and buttons == _dg_buttons(m, "digest2")
    assert _to(ch, CAPTAIN)[3] == ("7 Петров Александр", None)

    for now in (START - timedelta(minutes=1), START, START + timedelta(hours=1)):
        await weekly.tick(session, ch, now=now)
    assert len(_to(ch, CAPTAIN)) == 4 and len(_to(ch, VICE)) == 4


async def test_woke_up_late_only_final_digest(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    await weekly.tick(session, ch, now=START - timedelta(hours=1))
    [(digest, buttons)] = _to(ch, CAPTAIN)  # без «Буду» — заявки вторым сообщением нет
    assert digest.startswith(D2) and weekly.APPLICATION_HINT not in digest
    assert buttons == _dg_buttons(m, "digest2")
    assert await notify.was_sent(session, tenant.id, "digest24_skipped", weekly.cycle_key(m)), (
        "суточная помечена пропущенной"
    )
    assert not await notify.was_sent(session, tenant.id, "digest24", weekly.cycle_key(m)), (
        "«digest24» — только если ушла"
    )
    await weekly.tick(session, ch, now=START - timedelta(minutes=30))
    assert len(_to(ch, CAPTAIN)) == 1


async def test_no_digest_after_start(session, tenant_cfg):
    _, entries = await _ready(session, tenant_cfg)
    await _week_match(session, entries["abl"], "g1")
    ch = FakeChannel()
    await weekly.tick(session, ch, now=START + timedelta(minutes=1))
    assert ch.sent == []
    assert (await session.scalars(select(NotificationLog))).all() == []


async def test_digest_without_going_has_no_application(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    await _vote(session, tenant.id, m, await _player(session, tenant, SIDOROV), "not_going")
    ch = FakeChannel()
    await weekly.tick(session, ch, now=START - timedelta(hours=24))
    [(digest, _)] = _to(ch, CAPTAIN)
    assert "Не буду (1): Сидоров Евгений" in digest
    assert weekly.APPLICATION_HINT not in digest


# ---------- заявка на игру ----------


async def test_application_text_sorted_override_no_warnings(session, tenant_cfg):
    tenant, _, m = await _full(session, tenant_cfg)
    # Ёлкин без номера, Капитанов №1, Петров — правка 77 важнее №7; ⚠️-игроки и «Не буду» не попадают
    assert await digests.application_text(session, tenant.id, m) == (
        "? Ёлкин Пётр\n1 Капитанов Иван\n77 Петров Александр"
    )


async def test_application_text_none_when_nobody(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _week_match(session, entries["abl"], "g1")
    assert await digests.application_text(session, tenant.id, m) is None
    await _vote(session, tenant.id, m, await _player(session, tenant, "13"))  # Друг Команды — не сопоставлен
    assert await digests.application_text(session, tenant.id, m) is None


# ---------- изменения после сводки за 24 ч ----------

CHANGE = "Изменение в составе: "


async def test_vote_changed_only_after_digest24(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "g1", days=1)
    petrov = await _player(session, tenant, PETROV)
    ch = FakeChannel()
    await weekly.vote_changed(session, ch, tenant.id, m, petrov, "Буду")
    assert ch.sent == []

    await notify.once(session, tenant.id, "digest24", weekly.cycle_key(m), match_id=m.id)
    await session.commit()
    await weekly.vote_changed(session, ch, tenant.id, m, petrov, "Буду")
    for peer in (CAPTAIN, VICE):
        assert _texts(ch, peer) == [f"{CHANGE}Петров Александр — «Буду» (Соперник g1 — Westchester, АБЛ)."]
    assert _texts(ch, PETROV) == []

    captain = await _player(session, tenant, CAPTAIN)
    await weekly.vote_changed(session, ch, tenant.id, m, captain, "Не буду")
    assert len(_texts(ch, CAPTAIN)) == 1, "о своём голосе руководителю не пишем"
    assert _texts(ch, VICE)[-1].startswith(f"{CHANGE}Капитанов Иван — «Не буду»")


async def test_vote_changed_silent_after_start(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "g1", days=-0.01)
    await notify.once(session, tenant.id, "digest24", weekly.cycle_key(m), match_id=m.id)
    await session.commit()
    ch = FakeChannel()
    await weekly.vote_changed(session, ch, tenant.id, m, await _player(session, tenant, PETROV), "Буду")
    assert ch.sent == []


def _press(m: Match, user: str, vote: str) -> ButtonPress:
    return ButtonPress(CHAT, user, MessageRef(CHAT, "101"), {"m": m.id, "v": vote}, event_id="e")


async def test_vote_changed_via_on_vote(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "g1", days=1)
    await _publish(session, m)
    ch = FakeChannel()
    await handle(session, ch, _press(m, PETROV, "going"))
    assert _texts(ch, CAPTAIN) == [], "до сводки за 24 ч — тишина"

    await notify.once(session, tenant.id, "digest24", weekly.cycle_key(m), match_id=m.id)
    await session.commit()
    await handle(session, ch, _press(m, PETROV, "not_going"))
    await handle(session, ch, _press(m, PETROV, "not_going"))  # повторное нажатие — не изменение
    await handle(session, ch, _press(m, SIDOROV, "going"))
    for peer in (CAPTAIN, VICE):
        assert _texts(ch, peer, CHANGE) == [
            f"{CHANGE}Петров Александр — «Не буду» (Соперник g1 — Westchester, АБЛ).",
            f"{CHANGE}Сидоров Евгений — «Буду» (Соперник g1 — Westchester, АБЛ).",
        ]
    assert _texts(ch, PETROV, CHANGE) == [] and _texts(ch, SIDOROV, CHANGE) == []


# ---------- кнопка «Обновить» под сводкой ----------


async def test_digest_refresh_button(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "g1", days=1)
    ch = FakeChannel()
    ref = MessageRef(CAPTAIN, "500")
    await handle(session, ch, ButtonPress(CAPTAIN, CAPTAIN, ref, {"a": "dg", "m": m.id, "k": "digest2"}, "e"))
    assert ch.toasts == ["Обновлено"]
    assert ch.edits[-1][0] == ref and ch.sent == []
    assert ch.messages[(CAPTAIN, "500")].startswith(f"{D2}\n\n🏀 АБЛ: Соперник g1 — Westchester")
    assert ch.buttons[(CAPTAIN, "500")] == _dg_buttons(m, "digest2")


async def test_digest_refresh_foreign_or_missing_match(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    b, _, _ = await _second_tenant(session, entries)
    own_b = (
        await session.scalars(
            select(TeamEntry).where(
                TeamEntry.tenant_id == b.id, TeamEntry.league_team_id != entries["abl"].league_team_id
            )
        )
    ).one()
    foreign = await _rel_match(session, own_b, "b1", days=1)  # матч ВЛБЛ — только у команды B
    ch = FakeChannel()
    for mid in (foreign.id, 99999):
        await handle(
            session, ch, ButtonPress(CAPTAIN, CAPTAIN, MessageRef(CAPTAIN, "500"), {"a": "dg", "m": mid}, "e")
        )
    assert ch.toasts == ["Матч не найден", "Матч не найден"]
    assert ch.edits == [] and ch.sent == []


# ---------- мультитенантность ----------


async def test_isolated_between_tenants(session, tenant_cfg):
    """У B выступление в той же команде АБЛ: матч общий, но опрос, отметки и сводки — свои."""
    tenant, entries = await _ready(session, tenant_cfg)
    b, gromov, _ = await _second_tenant(session, entries)
    m = await _week_match(session, entries["abl"], "g1")
    await _vote(session, tenant.id, m, await _player(session, tenant, SIDOROV))
    await _vote(session, b.id, m, gromov)
    ch = FakeChannel()
    await weekly.tick(session, ch, now=at(WED, 10))
    assert len(_to(ch, CHAT)) == 1, "анонс — только в беседе A; у B беседы нет"
    assert [p.tenant_id for p in (await session.scalars(select(PollMessage))).all()] == [tenant.id]

    await weekly.tick(session, ch, now=START - timedelta(hours=24))
    a_app = _to(ch, CAPTAIN)[-1][0]
    b_digest, b_app = (t for t, _ in _to(ch, "900"))
    assert a_app == "11 Сидоров Евгений"
    assert b_app == "7 Петров Александр"  # Громов в B сопоставлен со строкой a1
    assert "Сидоров" not in b_digest and "Капитанов" not in b_digest
    assert "Громов" not in "".join(t for t, _ in _to(ch, CAPTAIN) + _to(ch, VICE))
    for t in (tenant.id, b.id):
        assert await notify.was_sent(session, t, "digest24", weekly.cycle_key(m))


async def test_leader_on_button_dg_directly(session, tenant_cfg):
    """Ветка «dg» в leader.on_button: обновлённая сводка, чужой и несуществующий матч — «Матч не найден»."""
    tenant, entries = await _ready(session, tenant_cfg)
    b, _, _ = await _second_tenant(session, entries)
    m = await _rel_match(session, entries["abl"], "g1", days=1)
    own_b = (
        await session.scalars(
            select(TeamEntry).where(
                TeamEntry.tenant_id == b.id, TeamEntry.league_team_id != entries["abl"].league_team_id
            )
        )
    ).one()
    foreign = await _rel_match(session, own_b, "b1", days=1)
    captain = await _player(session, tenant, CAPTAIN)
    ch = FakeChannel()

    def press(payload):
        return ButtonPress(CAPTAIN, CAPTAIN, MessageRef(CAPTAIN, "500"), payload, "e")

    text, buttons = await leader.on_button(session, ch, tenant.id, captain, press({"a": "dg", "m": m.id}))
    assert text.startswith(f"{D24}\n\n🏀 АБЛ: Соперник g1 — Westchester")  # без «k» — суточная
    assert buttons == _dg_buttons(m, "digest24")
    text, buttons = await leader.on_button(
        session, ch, tenant.id, captain, press({"a": "dg", "m": m.id, "k": "digest2"})
    )
    assert text.startswith(D2) and buttons == _dg_buttons(m, "digest2")
    for mid in (foreign.id, 99999):
        assert await leader.on_button(session, ch, tenant.id, captain, press({"a": "dg", "m": mid})) is None
    assert ch.toasts == ["Обновлено", "Обновлено", "Матч не найден", "Матч не найден"]

    petrov = await _player(session, tenant, PETROV)  # не руководитель
    assert await leader.on_button(session, ch, tenant.id, petrov, press({"a": "dg", "m": m.id})) is None
    assert ch.toasts[-1] == "Это доступно только руководителям команды"
