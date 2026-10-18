"""Недельный цикл матча: запуск опроса, «время не назначено», сводки руководителям (domain.md, architecture.md §5).

Вызывается планировщиком раз в минуту; работает только по БД (сайты лиг не трогает). Каждое действие
выполняется один раз — ключ в notification_log, поэтому перезапуск бота ничего не дублирует.
- Опрос: с «среды перед игрой» (League.poll_weekday / poll_time, по умолчанию ср 10:00 по времени лиги),
  только если время известно. Время появилось позже — опрос сразу, на ближайшей минуте.
- Без времени опроса нет: руководителям «время не назначено — свяжитесь с лигой», раз в сутки.
- Сводки за 24 ч и за 2 ч до начала: состав на матч + заявка на игру отдельным сообщением для копирования.
- После сводки за 24 ч каждое изменение ответа — короткое сообщение руководителям (`vote_changed`).
"""

import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.channels.base import Button, ChannelAdapter, ChannelError
from basket_assist.services import digests, leaders, notify, polls
from basket_assist.services.schedule import FINISHED, format_when, teams
from basket_assist.storage.tables import Chat, League, LeagueTeam, Match, Player, TeamEntry

log = logging.getLogger(__name__)

DIGESTS = {  # вид сводки → (за сколько до начала, заголовок)
    "digest24": (timedelta(hours=24), "📋 Сводка за сутки до игры"),
    "digest2": (timedelta(hours=2), "📋 Финальная сводка: игра через 2 часа"),
}
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
                    Match.match_date >= today,
                    or_(Match.start_at.is_(None), Match.start_at > now_utc),
                )
                .order_by(Match.match_date)
            )
        ).all()
        for match in matches:
            if now_utc < poll_opens_at(match, league):
                continue
            if match.start_at is None:
                await _warn_no_time(session, channel, entry.tenant_id, match, league, today)
            else:
                await _ensure_poll(session, channel, entry.tenant_id, match, now_utc)
                await _send_digests(session, channel, entry.tenant_id, match, now_utc)


async def _warn_no_time(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, match: Match, league: League, today
) -> None:
    if not await notify.once(session, tenant_id, "no_time", f"{match.id}:{today}", match_id=match.id):
        return
    await session.commit()
    _, ours, rival, _ = await polls._context(session, match, tenant_id)
    text = (
        f"⏰ Время матча не назначено: {teams(match, ours, rival)} ({league.name}), {format_when(match, league)}.\n"
        "Опрос не запускается, пока лиги нет времени. Свяжитесь с лигой. Напомню завтра, если ничего не изменится."
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
        session, tenant_id, "poll_try", f"{match.id}:{now_utc:%Y-%m-%dT%H}", match_id=match.id
    ):
        return
    await session.commit()
    chat = (
        (await session.execute(select(Chat).filter_by(tenant_id=tenant_id, channel=channel.channel)))
        .scalars()
        .first()
    )
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
        stale = kind == "digest24" and now_utc >= match.start_at - DIGESTS["digest2"][0]
        if not await notify.once(session, tenant_id, kind, str(match.id), match_id=match.id):
            continue
        await session.commit()
        if not stale:
            await send_digest(session, channel, tenant_id, match, kind)


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
    """Текст сводки и кнопка [Обновить] (перерисовывает это же сообщение, leader.on_button «dg»)."""
    title = DIGESTS.get(kind, DIGESTS["digest24"])[1]
    text = await digests.match_digest(session, tenant_id, match, title)
    return text, [[Button("Обновить", {"a": "dg", "m": match.id, "k": kind}, "secondary")]]


async def vote_changed(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, match: Match, player: Player, label: str
) -> None:
    """После сводки за 24 ч — каждое изменение ответа руководителям (кроме самого проголосовавшего)."""
    if not await notify.was_sent(session, tenant_id, "digest24", str(match.id)):
        return
    if match.start_at is None or match.start_at <= datetime.now(UTC).replace(tzinfo=None):
        return
    league, ours, rival, entry = await polls._context(session, match, tenant_id)
    name = await polls.player_name(session, tenant_id, player, entry)
    text = f"Изменение в составе: {name} — «{label}» ({teams(match, ours, rival)}, {league.name})."
    for lead in await leaders.digest_recipients(session, tenant_id):
        if lead.id != player.id:
            await notify.dm(session, channel, tenant_id, lead.id, text)
