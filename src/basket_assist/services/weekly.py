"""Недельный цикл матча: запуск опроса, «время не назначено», сводки руководителям (domain.md, architecture.md §5).

Вызывается планировщиком раз в минуту; работает только по БД (сайты лиг не трогает). Каждое действие
выполняется один раз — ключ в notification_log, поэтому перезапуск бота ничего не дублирует.
- Опрос: с «среды перед игрой» (League.poll_weekday / poll_time, по умолчанию ср 10:00 по времени лиги),
  только если время известно. Время появилось позже — опрос сразу, на ближайшей минуте.
- Без времени опроса нет: руководителям «время не назначено — свяжитесь с лигой» — один раз и накануне игры, если время так и не появилось.
- Сводки за 24 ч и за 2 ч до начала: состав на матч + заявка на игру отдельным сообщением для копирования.
- За 30 ч — напоминание не ответившим: в личку с кнопками или упоминанием в беседе.
- Тихие часы 22:00–10:00 МСК (ADR 0008): опрос, напоминание, «время не назначено» ждут утра.
"""

import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.channels.base import Button, ChannelAdapter, ChannelError
from basket_assist.services import chats, digests, leaders, notify, polls, quiet, roster
from basket_assist.services.schedule import FINISHED, format_when, teams
from basket_assist.storage.tables import (
    Attendance,
    League,
    LeagueTeam,
    Match,
    MessengerAccount,
    Player,
    TeamEntry,
)

log = logging.getLogger(__name__)

DIGESTS = {  # вид сводки → (за сколько до начала, заголовок)
    "digest24": (timedelta(hours=24), "📋 Сводка за сутки до игры"),
    "digest2": (timedelta(hours=2), "📋 Финальная сводка: игра через 2 часа"),
}
APP_TITLE = "📝 Состав и заявка на игру"  # карточка по запросу (кнопка «Состав и заявка»)
COPY_BUTTON = "📋 Текст"  # текст для копирования; в паре — не длиннее PAIR_LABEL_MAX
REMIND_BEFORE = timedelta(hours=30)  # напоминание молчащим (architecture.md §5)
REMIND_AFTER_POLL = timedelta(hours=3)  # опрос запущен поздно — не напоминаем в ту же минуту, что и анонс


def cycle_key(match: Match) -> str:
    """Ключ журнала для напоминания и сводок: матч + дата + время. Перенос и смена времени — новый цикл:
    опрос перезапущен (отметки сброшены), напоминание и сводки уходят заново (решение владельца 2026-10-20)."""
    key = f"{match.id}:{match.match_date.isoformat()}"
    return f"{key}T{match.start_at:%H:%M}" if match.start_at else key


NUMBER_HINT = "Нет номера или он другой — напишите «номер Петров 23»."
APPLICATION_HINT = "Следующим сообщением — заявка на игру (номер и ФИО), его удобно скопировать целиком."


def poll_opens_at(match: Match, league: League) -> datetime:
    """Когда открывается опрос (UTC без tzinfo): ближайший League.poll_weekday не позже дня игры.

    Для игры в пн или вт это среда предыдущей недели (domain.md, ⚠ редкий случай).
    """
    tz = ZoneInfo(league.timezone)
    day = match.match_date - timedelta(days=(match.match_date.weekday() - league.poll_weekday) % 7)
    hh, mm = (int(x) for x in league.poll_time.split(":"))
    return datetime.combine(day, time(hh, mm), tz).astimezone(UTC).replace(tzinfo=None)


async def tick(session: AsyncSession, channel: ChannelAdapter, now: datetime | None = None) -> None:
    now_utc = (now or datetime.now(UTC)).astimezone(UTC).replace(tzinfo=None)
    for tenant_id in (await session.scalars(select(TeamEntry.tenant_id).distinct())).all():
        await polls.relocate(
            session, channel, tenant_id, now_utc
        )  # переезд беседы: до проверки «анонс уже есть»
    for entry in (await session.scalars(select(TeamEntry).order_by(TeamEntry.id))).all():
        team = await session.get(LeagueTeam, entry.league_team_id)
        league = await session.get(League, team.league_id)
        today = now_utc.replace(tzinfo=UTC).astimezone(ZoneInfo(league.timezone)).date()
        matches = (
            await session.scalars(
                select(Match)
                .where(
                    Match.tournament_id == entry.tournament_id,
                    or_(Match.home_team_id == team.id, Match.away_team_id == team.id),
                    Match.status.not_in(FINISHED),
                    Match.date_is_estimate.is_(False),  # черновик без даты: ни опроса, ни предупреждений
                    Match.match_date >= today,
                    or_(Match.start_at.is_(None), Match.start_at > now_utc),
                )
                .order_by(Match.match_date)
            )
        ).all()
        for match in matches:
            if now_utc < poll_opens_at(match, league):
                continue
            # тихие часы (ADR 0008): анонс, напоминание и «время не назначено» ждут 10:00; сводки — по расписанию
            quiet_now = quiet.is_quiet(now_utc)
            if match.start_at is None:
                if not quiet_now:
                    await _warn_no_time(session, channel, entry.tenant_id, match, league, today, now_utc)
            else:
                if not quiet_now:
                    await _ensure_poll(session, channel, entry.tenant_id, match, now_utc)
                    await _remind_silent(session, channel, entry, match, now_utc)
                await _send_digests(session, channel, entry.tenant_id, match, now_utc)


async def _remind_silent(
    session: AsyncSession, channel: ChannelAdapter, entry: TeamEntry, match: Match, now_utc: datetime
) -> None:
    """За 30 ч до начала — напоминание заявленным на сезон, кто ещё не ответил.

    С открытой личкой — в личку с кнопками «Буду / Не буду» (нажатие = голос в анонсе);
    остальным — одно сообщение в беседу с упоминаниями (domain.md, «Подключение игрока к боту»).
    """
    tenant_id = entry.tenant_id
    if now_utc < match.start_at - REMIND_BEFORE:
        return  # рано: дешёвая проверка до запроса в БД (тик — каждую минуту)
    poll = await polls._current_poll(session, tenant_id, match.id)
    if poll is None:
        return  # без анонса напоминать не о чем; попробуем на следующей минуте
    remind_at = max(match.start_at - REMIND_BEFORE, poll.posted_at + REMIND_AFTER_POLL)
    if not (remind_at <= now_utc < match.start_at - DIGESTS["digest2"][0]):
        return
    if not await notify.once(session, tenant_id, "remind30", cycle_key(match), match_id=match.id):
        return
    await session.commit()
    league, ours, rival, _ = await polls._context(session, match, tenant_id)
    voted = set(
        (
            await session.scalars(
                select(Attendance.player_id).filter_by(tenant_id=tenant_id, match_id=match.id)
            )
        ).all()
    )
    what = f"{teams(match, ours, rival)} ({league.name}), {format_when(match, league)}"
    no_dm: list[tuple[str, str]] = []  # (id в мессенджере, имя) — личка не открыта
    for p in (await session.scalars(select(Player).filter_by(tenant_id=tenant_id, active=True))).all():
        if p.id in voted or await roster.roster_status(session, tenant_id, p.id, entry.id) != "in_roster":
            continue
        text = f"⏰ Напоминание: {what}.\nВы ещё не ответили — будете? Ответ можно поменять до начала игры."
        if not await notify.dm(session, channel, tenant_id, p.id, text, polls.vote_buttons(match.id)):
            account = (
                await session.execute(
                    select(MessengerAccount).filter_by(
                        tenant_id=tenant_id, player_id=p.id, channel=channel.channel
                    )
                )
            ).scalar_one_or_none()
            if account is not None:
                no_dm.append(
                    (account.external_user_id, await polls.player_name(session, tenant_id, p, entry))
                )
    chat = await chats.active_chat(session, tenant_id, channel.channel)
    if not no_dm or chat is None:
        return
    # упоминаем только участников беседы: иначе из тестовой беседы потревожим настоящих игроков
    try:
        members = set(await channel.chat_members(chat.peer_id))
    except ChannelError as e:
        log.warning("не получил участников беседы для напоминания (%s) — без упоминаний", e)
        return
    in_chat = [channel.mention(uid, name) for uid, name in no_dm if uid in members]
    if in_chat:
        try:
            await channel.send_with_mentions(
                chat.peer_id,
                f"⏰ {what}. Ещё не ответили: {', '.join(in_chat)}.\nОтметьтесь в закреплённом анонсе — «Буду» или «Не буду».",
            )
        except ChannelError as e:
            log.warning("напоминание в беседу по матчу %s не отправлено: %s", match.id, e)


async def _warn_no_time(
    session: AsyncSession,
    channel: ChannelAdapter,
    tenant_id: int,
    match: Match,
    league: League,
    today,
    now_utc: datetime,
) -> None:
    # повтор — раз в сутки в то же время, что и опрос (по умолчанию 10:00 по времени лиги), не ночью
    hh, mm = (int(x) for x in league.poll_time.split(":"))
    if now_utc.replace(tzinfo=UTC).astimezone(ZoneInfo(league.timezone)).time() < time(hh, mm):
        return
    # один раз и накануне игры, если время так и не появилось (решение владельца 2026-10-24, ADR 0008);
    # совпали (первое — уже накануне) — одно сообщение
    eve = today == match.match_date - timedelta(days=1)
    keys = ["first", "eve"] if eve else ["first"]
    new = [
        k
        for k in keys
        if await notify.once(session, tenant_id, "no_time", f"{match.id}:{k}", match_id=match.id)
    ]
    if not new:
        return
    await session.commit()
    _, ours, rival, _ = await polls._context(session, match, tenant_id)
    example = f"время {match.match_date:%d.%m} 18:30"
    text = (
        f"⏰ Время матча не назначено: {teams(match, ours, rival)} ({league.name}), {format_when(match, league)}.\n"
        "Опрос не запускается, пока у матча нет времени. Свяжитесь с лигой. "
        f"Если лига сообщила время, а на сайте его нет — напишите «{example}» "
        "(со своим временем)." + ("" if eve else " Напомню накануне игры, если время так и не появится.")
    )
    for lead in await leaders.digest_recipients(session, tenant_id):
        await notify.dm(session, channel, tenant_id, lead.id, text)


async def _ensure_poll(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, match: Match, now_utc: datetime
) -> None:
    if await polls._current_poll(session, tenant_id, match.id) is not None:
        return
    # попытка публикации — не чаще раза в час: при сбое VK не долбим каждую минуту
    if not await notify.once(
        session, tenant_id, "poll_try", f"{cycle_key(match)}:{now_utc:%Y-%m-%dT%H}", match_id=match.id
    ):
        return
    await session.commit()
    chat = await chats.active_chat(session, tenant_id, channel.channel)
    if chat is None:
        log.warning("опрос по матчу %s не запущен: у команды %s нет беседы", match.id, tenant_id)
        return
    try:
        await polls.publish(session, channel, chat, match)
    except ChannelError as e:
        log.error("не удалось опубликовать анонс матча %s: %s", match.id, e)
        return
    log.info("опрос по матчу %s запущен", match.id)


async def _send_digests(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, match: Match, now_utc: datetime
) -> None:
    for kind, (before, _title) in DIGESTS.items():
        if not (match.start_at - before <= now_utc < match.start_at):
            continue
        # бот был выключен и проснулся в последние 2 ч: суточную только отмечаем, хватит финальной
        # (отдельный ключ: «digest24» значит, что суточная правда ушла — от неё считаются 🆕 и изменения)
        stale = kind == "digest24" and now_utc >= match.start_at - DIGESTS["digest2"][0]
        key_kind = "digest24_skipped" if stale else kind
        if not await notify.once(session, tenant_id, key_kind, cycle_key(match), match_id=match.id):
            continue
        await session.commit()
        if not stale:
            await send_digest(session, channel, tenant_id, match, kind)


async def application_card(
    session: AsyncSession, tenant_id: int, match: Match
) -> list[tuple[str, list[list[Button]] | None]]:
    """Карточка по запросу («Состав и заявка»): кнопки правки; текст для копирования — по кнопке «📋»."""
    return [await digest_message(session, tenant_id, match, "app")]


async def send_digest(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, match: Match, kind: str
) -> None:
    text, buttons = await digest_message(session, tenant_id, match, kind)
    application = await digests.application_text(session, tenant_id, match)
    if application:
        text += f"\n\n{APPLICATION_HINT}"
    for lead in await leaders.digest_recipients(session, tenant_id):
        if await notify.dm(session, channel, tenant_id, lead.id, text, buttons) and application:
            await notify.dm(session, channel, tenant_id, lead.id, application)


async def digest_message(
    session: AsyncSession, tenant_id: int, match: Match, kind: str
) -> tuple[str, list[list[Button]]]:
    """Текст сводки и кнопки: [➕ Добавить] [➖ Убрать], [🔄 Обновить] [📋 Текст] (leader.on_button).

    kind: digest24 / digest2 — сводки по расписанию; app — карточка по запросу («Состав и заявка»).
    «📋» шлёт свежий текст для копирования: после правок текст, пришедший со сводкой, устаревает.
    """
    title = APP_TITLE if kind == "app" else DIGESTS.get(kind, DIGESTS["digest24"])[1]
    # финальная: изменения после сводки за сутки отмечены (если суточная была)
    since = (
        await notify.sent_at(session, tenant_id, "digest24", cycle_key(match)) if kind == "digest2" else None
    )
    text = await digests.match_digest(session, tenant_id, match, title, since)
    text += f"\n\n{NUMBER_HINT}"
    ref = {"m": match.id, "k": kind}
    return text, [
        [Button("➕ Добавить", {"a": "aa", **ref}), Button("➖ Убрать", {"a": "ar", **ref})],
        [
            Button("🔄 Обновить", {"a": "dg", **ref}, "secondary"),
            Button(COPY_BUTTON, {"a": "ac", "m": match.id}, "secondary"),
        ],
    ]
