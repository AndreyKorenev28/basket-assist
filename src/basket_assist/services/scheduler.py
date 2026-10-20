"""Минутный планировщик по состоянию БД (architecture.md, «Недельный цикл»).

Сейчас: синхронизация с сайтами лиг и реакция на изменения матчей.
- Раз в сутки ночью — полная синхронизация (календарь, заявка на сезон, таблица).
- Ср–Пт, каждый час 9:00–23:00 по времени лиги — только календарь (решение владельца 2026-10-18:
  с утра среды, к запуску опросов; в выходные переносов не бывает; АБЛ публикует тур во вторник — его подхватит ночная синхронизация).
- Через 3 ч после начала нашего матча — календарь и таблица, чтобы счёт был в тот же вечер.
- После синхронизации: анонсы изменившихся матчей обновляются; о переносе и смене времени — сообщение в беседу.
Факт запуска хранится в notification_log (kind = "job"), поэтому перезапуск бота не повторяет синхронизацию.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from basket_assist.channels.base import ChannelAdapter, ChannelError
from basket_assist.domain.models import ChangeKind
from basket_assist.services import chats, notify, polls, weekly
from basket_assist.services.schedule import FINISHED, format_when, teams
from basket_assist.services.sync import sync_team_entry
from basket_assist.sources import make_adapter
from basket_assist.sources.base import SourceError
from basket_assist.storage.tables import (
    ChangeEvent,
    League,
    LeagueTeam,
    Match,
    PollMessage,
    TeamEntry,
    utcnow,
)

log = logging.getLogger(__name__)

TICK = 60  # секунд
NIGHT_HOUR = 4  # полная синхронизация — первая проверка после 04:00 по времени лиги
HOURLY_DAYS = (2, 3, 4)  # ср–пт (понедельник = 0); в выходные переносов не бывает (решение владельца)
HOURLY_FROM, HOURLY_TO = 9, 23
RESULT_AFTER = timedelta(hours=3)  # через сколько после начала нашего матча забрать счёт
RESULT_WINDOW = timedelta(hours=12)  # бот был выключен дольше — ждём ночной синхронизации
MATCH_KINDS = (ChangeKind.RESCHEDULED, ChangeKind.TIME_CHANGED, ChangeKind.TIME_SET, ChangeKind.MATCH_ADDED)
RESTART_KINDS = (ChangeKind.RESCHEDULED, ChangeKind.TIME_CHANGED)  # новый опрос: отметки сбрасываются


def due_jobs(local: datetime) -> list[tuple[str, str]]:
    """Синхронизации, положенные в этот момент (время лиги): [(вид, ключ слота)], полная — первой.

    Слот выполняется один раз: полная — раз в сутки после NIGHT_HOUR, календарь — раз в час в окне.
    """
    jobs = []
    if local.hour >= NIGHT_HOUR:
        jobs.append(("full", f"full:{local:%Y-%m-%d}"))
    if local.weekday() in HOURLY_DAYS and HOURLY_FROM <= local.hour <= HOURLY_TO:
        jobs.append(("matches", f"matches:{local:%Y-%m-%dT%H}"))
    return jobs


async def _awaiting_result(session: AsyncSession, entry: TeamEntry, now: datetime) -> list[int]:
    """Наши матчи, начавшиеся RESULT_AFTER назад (но не раньше RESULT_WINDOW) и ещё без результата.

    По каждому — одна синхронизация календаря и таблицы, чтобы счёт появился в тот же вечер
    (решение владельца 2026-10-18). Не успел появиться на сайте — подхватит ночная синхронизация.
    """
    naive = now.astimezone(UTC).replace(tzinfo=None)
    return list(
        (
            await session.scalars(
                select(Match.id).where(
                    Match.tournament_id == entry.tournament_id,
                    or_(
                        Match.home_team_id == entry.league_team_id, Match.away_team_id == entry.league_team_id
                    ),
                    Match.status.not_in(FINISHED),
                    Match.start_at <= naive - RESULT_AFTER,
                    Match.start_at > naive - RESULT_WINDOW,
                )
            )
        ).all()
    )


async def run(sessions: async_sessionmaker, channel: ChannelAdapter, client: httpx.AsyncClient) -> None:
    """Фоновая задача бота: при старте — обновить анонсы, дальше — проверка раз в минуту."""
    try:
        async with sessions() as session:
            await refresh_active_polls(session, channel)
    except Exception:
        log.exception("не удалось обновить анонсы при запуске")
    while True:
        try:
            async with sessions() as session:
                await tick(session, channel, client)
        except Exception:
            log.exception("ошибка планировщика")
        try:  # отдельно: сбой синхронизации не должен мешать опросам и сводкам
            async with sessions() as session:
                await weekly.tick(session, channel)
        except Exception:
            log.exception("ошибка недельного цикла")
        await asyncio.sleep(TICK)


async def tick(
    session: AsyncSession, channel: ChannelAdapter, client: httpx.AsyncClient, now: datetime | None = None
) -> None:
    now = now or datetime.now(UTC)
    synced = False
    for entry in (await session.scalars(select(TeamEntry).order_by(TeamEntry.id))).all():
        team = await session.get(LeagueTeam, entry.league_team_id)
        league = await session.get(League, team.league_id)
        # занимаем все положенные слоты разом: полная включает календарь, повторять его следом не нужно
        slots = due_jobs(now.astimezone(ZoneInfo(league.timezone)))
        slots += [("result", f"result:{m_id}") for m_id in await _awaiting_result(session, entry, now)]
        new = [
            kind
            for kind, slot in slots
            if await notify.once(session, entry.tenant_id, "job", f"{entry.id}:{slot}")
        ]
        job = new[0] if new else None  # полная идёт первой и включает всё остальное
        if job is None:
            continue
        await session.commit()  # слот занят сразу: при падении сайта не долбим его каждую минуту
        adapter = make_adapter(league.platform, client, league.store_birth_date)
        try:
            r = await sync_team_entry(
                session,
                entry,
                adapter,
                matches=True,
                roster=job == "full",
                standings=job in ("full", "result"),
            )
        except (SourceError, httpx.HTTPError) as e:
            await session.rollback()
            log.error("[%s] синхронизация (%s) не удалась: %s", league.key, job, e)
            continue
        synced = True
        log.info(
            "[%s] синхронизация (%s): новых матчей %s, изменено %s, изменений %s",
            league.key,
            job,
            r.matches_new,
            r.matches_updated,
            len(r.changes),
        )
    await process_changes(session, channel)  # каждую минуту: и ручная смена времени руководителем
    if synced:
        await refresh_active_polls(session, channel, only_changed=True)


async def process_changes(session: AsyncSession, channel: ChannelAdapter) -> None:
    """Необработанные изменения матчей → сообщение в беседу команды. Изменения заявки на сезон не трогаем.

    Перенос даты или новое время — по сути новый опрос (решение владельца 2026-10-20): отметки
    сбрасываются, старый анонс закрывается, новый публикует недельный цикл; всем — сообщение в беседе,
    отметившимся — ещё и в личку с кнопками.
    """
    events = (
        await session.scalars(
            select(ChangeEvent)
            .where(ChangeEvent.processed.is_(False), ChangeEvent.kind.in_(MATCH_KINDS))
            .order_by(ChangeEvent.id)
        )
    ).all()
    restarted: set[tuple[int, int]] = set()  # перенос и новое время в одной синхронизации — один перезапуск
    for ev in events:
        ev.processed = True
        match = await session.get(Match, ev.match_id) if ev.match_id else None
        if match is None or match.status in FINISHED or match.date_is_estimate:  # черновик без даты — молчим
            continue
        for entry in await _entries_for(session, match):
            text = await _change_text(session, ev, match, entry.tenant_id)
            if text is None:
                continue
            votes: list = []
            reopened = False
            if ev.kind in RESTART_KINDS and (entry.tenant_id, match.id) not in restarted:
                restarted.add((entry.tenant_id, match.id))
                reopened = await polls._current_poll(session, entry.tenant_id, match.id) is not None
                votes = await polls.restart(session, channel, entry.tenant_id, match)
                if reopened or votes:
                    text += " Прежние отметки сброшены — отметьтесь заново в закреплённом анонсе."
            chat = await chats.active_chat(session, entry.tenant_id, channel.channel)
            if chat is not None:
                try:
                    await channel.send(chat.peer_id, text)
                except ChannelError as e:
                    log.warning("не удалось сообщить об изменении матча %s: %s", match.id, e)
            await _ask_voters_again(session, channel, entry.tenant_id, ev, match, votes)
    await session.commit()


async def _ask_voters_again(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, ev: ChangeEvent, match: Match, votes: list
) -> None:
    """Отметившимся до переноса — личное сообщение: отметка сброшена, кнопки для нового ответа.
    Без открытой лички хватит сообщения в беседе и напоминания молчащим."""
    head = await _change_text(session, ev, match, tenant_id)
    for player_id, vote in votes:
        if not await notify.once(
            session, tenant_id, "match_change", f"{ev.id}:{player_id}", match_id=match.id
        ):
            continue
        await session.commit()
        await notify.dm(
            session,
            channel,
            tenant_id,
            player_id,
            f"{head}\nВаша отметка «{polls.VOTE_LABELS[vote]}» сброшена. Будете в новое время?",
            polls.vote_buttons(match.id),
        )


async def _change_text(session: AsyncSession, ev: ChangeEvent, match: Match, tenant_id: int) -> str | None:
    league, ours, rival, _ = await polls._context(session, match, tenant_id)
    what = f"{teams(match, ours, rival)} ({league.name})"
    when = format_when(match, league)
    if ev.kind == ChangeKind.RESCHEDULED:
        was = json.loads(ev.payload or "{}").get("was", "")
        was_text = f" Было: {datetime.fromisoformat(was):%d.%m}." if was else ""
        return f"⚠️ Перенос матча: {what} — теперь {when}.{was_text}"
    if ev.kind == ChangeKind.TIME_CHANGED:
        return f"⏰ Изменилось время матча: {what} — теперь {when}."
    if ev.kind == ChangeKind.TIME_SET:
        return f"⏰ Назначено время матча: {what} — {when}."
    if ev.kind == ChangeKind.MATCH_ADDED:
        return f"📅 В календаре лиги новый матч: {what} — {when}."
    return None


async def _entries_for(session: AsyncSession, match: Match) -> list[TeamEntry]:
    """Выступления команд сервиса, которые играют этот матч (в MVP — одно)."""
    return list(
        (
            await session.scalars(
                select(TeamEntry).where(
                    TeamEntry.tournament_id == match.tournament_id,
                    or_(
                        TeamEntry.league_team_id == match.home_team_id,
                        TeamEntry.league_team_id == match.away_team_id,
                    ),
                )
            )
        ).all()
    )


async def refresh_active_polls(
    session: AsyncSession, channel: ChannelAdapter, only_changed: bool = False
) -> int:
    """Перерисовать действующие анонсы предстоящих матчей. only_changed — только если матч менялся после
    последней правки анонса (дата, время, зал, состав команд). Возвращает число обновлённых."""
    cutoff = utcnow() - timedelta(hours=2)  # начавшийся матч ещё может обновиться (счёт), дальше — нет
    rows = (
        await session.execute(
            select(PollMessage, Match)
            .join(Match, Match.id == PollMessage.match_id)
            .where(
                PollMessage.superseded_by_id.is_(None),
                PollMessage.closed_at.is_(None),
                Match.status.not_in(FINISHED),
                or_(Match.start_at.is_(None), Match.start_at > cutoff),
            )
        )
    ).all()
    done = 0
    seen: set[tuple[int, str]] = set()  # общий анонс (несколько матчей) перерисовываем один раз
    for poll, match in rows:
        if only_changed and match.updated_at <= (poll.last_edited_at or poll.posted_at):
            continue
        if (poll.chat_id, poll.message_ref) in seen:
            continue
        seen.add((poll.chat_id, poll.message_ref))
        try:
            await polls.refresh(session, channel, poll.tenant_id, match)
            done += 1
        except ChannelError as e:
            log.warning("не удалось обновить анонс матча %s: %s", match.id, e)
    if done:
        log.info("обновлено анонсов: %s", done)
    return done
