"""Анонс-голосование «Буду / Не буду» (ADR 0004).

Анонс в беседе показывает имена отметившихся по каждому варианту и обновляется при каждом
нажатии вплоть до начала матча. Если VK не даёт отредактировать старый анонс, бот публикует
новый, закрепляет его, а старый помечает «устарел» — нажатия на старом продолжают учитываться.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import UTC
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.channels.base import Button, ButtonPress, ChannelAdapter, MessageRef
from basket_assist.domain.models import MatchStatus, Vote
from basket_assist.services.players import get_or_create_player
from basket_assist.storage.tables import (
    Attendance,
    Chat,
    League,
    LeagueTeam,
    Match,
    Player,
    PollMessage,
    TeamEntry,
    Tournament,
    utcnow,
)

log = logging.getLogger(__name__)

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
VOTE_LABELS = {Vote.GOING: "Буду", Vote.NOT_GOING: "Не буду"}
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class PollError(Exception):
    pass


def vote_buttons(match_id: int) -> list[list[Button]]:
    return [
        [
            Button("Буду", {"m": match_id, "v": Vote.GOING.value}, "positive"),
            Button("Не буду", {"m": match_id, "v": Vote.NOT_GOING.value}, "negative"),
        ]
    ]


async def _context(session: AsyncSession, match: Match, tenant_id: int):
    tournament = await session.get(Tournament, match.tournament_id)
    league = await session.get(League, tournament.league_id)
    home = await session.get(LeagueTeam, match.home_team_id)
    away = await session.get(LeagueTeam, match.away_team_id)
    entry = (
        await session.execute(
            select(TeamEntry).where(
                TeamEntry.tenant_id == tenant_id,
                TeamEntry.tournament_id == match.tournament_id,
                TeamEntry.league_team_id.in_([home.id, away.id]),
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise PollError(f"матч {match.id} — не наш")
    ours, rival = (home, away) if entry.league_team_id == home.id else (away, home)
    return league, ours, rival


async def render(session: AsyncSession, match: Match, tenant_id: int) -> str:
    league, ours, rival = await _context(session, match, tenant_id)
    tz = ZoneInfo(league.timezone)
    if match.start_at:
        local = match.start_at.replace(tzinfo=UTC).astimezone(tz)
        when = f"{WEEKDAYS[local.weekday()]} {local:%d.%m}, {local:%H:%M}"
    else:
        when = f"{WEEKDAYS[match.match_date.weekday()]} {match.match_date:%d.%m}, время не назначено"
    place = ", ".join(x for x in (match.venue_name, match.venue_address) if x) or "зал не указан"

    rows = (
        await session.execute(
            select(Attendance.vote, Player.display_name)
            .join(Player, Player.id == Attendance.player_id)
            .where(Attendance.tenant_id == tenant_id, Attendance.match_id == match.id)
            .order_by(Attendance.updated_at, Attendance.id)
        )
    ).all()
    by_vote: dict[str, list[str]] = {v: [] for v in VOTE_LABELS}
    for vote, name in rows:
        by_vote[vote].append(name)

    lines = [
        f"🏀 {league.name}: {ours.name} — {rival.name}",
        f"📅 {when}",
        f"📍 {place}",
        "",
        "Кто идёт? Отметьтесь кнопкой, ответ можно поменять до начала игры.",
        "",
    ]
    for vote, label in VOTE_LABELS.items():
        names = by_vote[vote]
        lines.append(f"{label} ({len(names)}): {', '.join(names) if names else '—'}")
    if match.status == MatchStatus.RESCHEDULED:
        lines.insert(1, "⚠️ Матч перенесён — проверьте дату и время.")
    return "\n".join(lines)


async def _current_poll(session: AsyncSession, tenant_id: int, match_id: int) -> PollMessage | None:
    return (
        (
            await session.execute(
                select(PollMessage)
                .where(
                    PollMessage.tenant_id == tenant_id,
                    PollMessage.match_id == match_id,
                    PollMessage.superseded_by_id.is_(None),
                )
                .order_by(PollMessage.id.desc())
            )
        )
        .scalars()
        .first()
    )


async def publish(session: AsyncSession, channel: ChannelAdapter, chat: Chat, match: Match) -> PollMessage:
    """Опубликовать анонс-голосование в беседе и закрепить. Повторный вызов вернёт существующий."""
    existing = await _current_poll(session, chat.tenant_id, match.id)
    if existing is not None:
        return existing
    text = await render(session, match, chat.tenant_id)
    ref = await channel.send(chat.peer_id, text, vote_buttons(match.id))
    poll = PollMessage(
        tenant_id=chat.tenant_id, match_id=match.id, chat_id=chat.id, message_ref=ref.message_id
    )
    session.add(poll)
    await session.commit()
    await channel.pin(ref)
    return poll


async def refresh(session: AsyncSession, channel: ChannelAdapter, tenant_id: int, match: Match) -> None:
    """Обновить анонс; если старый не редактируется — переопубликовать."""
    poll = await _current_poll(session, tenant_id, match.id)
    if poll is None:
        return
    chat = await session.get(Chat, poll.chat_id)
    text = await render(session, match, tenant_id)
    msg = MessageRef(chat.peer_id, poll.message_ref)
    if await channel.edit(msg, text, vote_buttons(match.id)):
        poll.last_edited_at = utcnow()
        await session.commit()
        return
    new_ref = await channel.send(chat.peer_id, text, vote_buttons(match.id))
    new_poll = PollMessage(
        tenant_id=tenant_id, match_id=match.id, chat_id=chat.id, message_ref=new_ref.message_id
    )
    session.add(new_poll)
    await session.flush()
    poll.superseded_by_id = new_poll.id
    await session.commit()
    await channel.pin(new_ref)
    await channel.edit(
        msg,
        "Этот анонс устарел — актуальный закреплён в беседе. Кнопки здесь тоже работают.",
        vote_buttons(match.id),
    )


async def on_vote(session: AsyncSession, channel: ChannelAdapter, tenant_id: int, press: ButtonPress) -> None:
    try:
        match_id, vote = int(press.payload["m"]), Vote(press.payload["v"])
    except (KeyError, ValueError, TypeError):
        log.warning("непонятная кнопка: %s", press.payload)
        return
    match = await session.get(Match, match_id)
    if match is None:
        await channel.answer_button(press, "Матч не найден")
        return
    if match.status in (MatchStatus.PLAYED, MatchStatus.CANCELLED) or (
        match.start_at is not None and match.start_at <= utcnow()
    ):
        await channel.answer_button(press, "Голосование закрыто — матч уже начался")
        return

    async with _locks[match_id]:
        player, _ = await get_or_create_player(session, tenant_id, channel, press.user_id)
        row = (
            await session.execute(
                select(Attendance).filter_by(tenant_id=tenant_id, match_id=match_id, player_id=player.id)
            )
        ).scalar_one_or_none()
        changed = row is None or row.vote != vote
        if row is None:
            session.add(Attendance(tenant_id=tenant_id, match_id=match_id, player_id=player.id, vote=vote))
        elif changed:
            row.vote, row.updated_at = vote, utcnow()
        await session.commit()
        await channel.answer_button(press, f"Отмечено: {VOTE_LABELS[vote]}")
        if changed:  # повторное нажатие той же кнопки ничего не меняет (domain.md)
            await refresh(session, channel, tenant_id, match)
