"""Анонс-голосование «Буду / Не буду» (ADR 0004).

Игры одной недели — в одном закреплённом сообщении (общий анонс, решение владельца 2026-10-20):
у каждой игры свои кнопки и списки. Технически — несколько PollMessage с одним message_ref.

Анонс в беседе показывает имена отметившихся по каждому варианту и обновляется при каждом
нажатии вплоть до начала матча. Если VK не даёт отредактировать старый анонс, бот публикует
новый, закрепляет его, а старый помечает «устарел» — нажатия на старом продолжают учитываться.
"""

import asyncio
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.channels.base import (
    PAIR_LABEL_MAX,
    Button,
    ButtonPress,
    ChannelAdapter,
    ChannelError,
    MessageRef,
)
from basket_assist.domain.models import MatchStatus, Vote
from basket_assist.services import chats, leaders, notify, roster
from basket_assist.services.players import get_or_create_player
from basket_assist.services.schedule import FINISHED, kit_hint, teams
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
# запасной путь, если кнопки анонса не работают (24.10: VK потерял кнопки у закрепа) — голос текстом в личке
TEXT_VOTE_HINT = "Кнопка не сработала? Напишите боту в личку «буду» или «не буду»."
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_msg_locks: defaultdict[tuple[int, str], asyncio.Lock] = defaultdict(asyncio.Lock)  # сообщение-анонс
GROUP_DAYS = 6  # в общий анонс — игры одной недели
MAX_IN_ANNOUNCE = 5  # VK: до 10 кнопок — по паре на игру


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
    return league, ours, rival, entry


async def player_name(session: AsyncSession, tenant_id: int, player: Player, entry: TeamEntry) -> str:
    """Имя для анонса и заявки: ФИО из заявки на сезон этой лиги, если игрок сопоставлен."""
    r = await roster.roster_entry_for(session, tenant_id, player.id, entry.id)
    return f"{r.last_name} {r.first_name}" if r else player.display_name


async def _block(session: AsyncSession, match: Match, tenant_id: int) -> tuple[list[str], list[str]]:
    """Матч в анонсе: (шапка — лига, команды, когда, зал, форма; строки «Буду / Не буду» с именами)."""
    league, ours, rival, entry = await _context(session, match, tenant_id)
    tz = ZoneInfo(league.timezone)
    if match.start_at:
        local = match.start_at.replace(tzinfo=UTC).astimezone(tz)
        when = f"{WEEKDAYS[local.weekday()]} {local:%d.%m}, {local:%H:%M}"
    else:
        when = f"{WEEKDAYS[match.match_date.weekday()]} {match.match_date:%d.%m}, время пока не назначено"
    place = ", ".join(x for x in (match.venue_name, match.venue_address) if x) or "зал не указан"

    rows = (
        await session.execute(
            select(Attendance.vote, Player)
            .join(Player, Player.id == Attendance.player_id)
            .where(Attendance.tenant_id == tenant_id, Attendance.match_id == match.id)
            .order_by(Attendance.updated_at, Attendance.id)
        )
    ).all()
    by_vote: dict[str, list[str]] = {v: [] for v in VOTE_LABELS}
    for vote, player in rows:
        by_vote[vote].append(await player_name(session, tenant_id, player, entry))

    head = [
        f"🏀 {league.name}: {teams(match, ours, rival)}",
        f"📅 {when}",
        place,
        kit_hint(match, ours),
    ]
    if match.status == MatchStatus.RESCHEDULED:
        head.insert(1, "⚠️ Матч перенесён — отметьтесь заново.")
    votes = []
    for vote, label in VOTE_LABELS.items():
        names = by_vote[vote]
        votes.append(f"{label} ({len(names)}): {', '.join(names) if names else '—'}")
    return head, votes


async def render(session: AsyncSession, match: Match, tenant_id: int) -> str:
    """Анонс одного матча."""
    head, votes = await _block(session, match, tenant_id)
    return "\n".join(
        [
            *head,
            "",
            "Кто идёт? Отметьтесь кнопкой, ответ можно поменять до начала игры.",
            "",
            *votes,
            "",
            TEXT_VOTE_HINT,
        ]
    )


def _open_for_votes(match: Match, now: datetime) -> bool:
    return match.status not in FINISHED and (match.start_at is None or match.start_at > now)


async def _tags(session: AsyncSession, matches: list[Match]) -> list[str]:
    """Метка игры на кнопках общего анонса: лига («МЛБЛ»), при двух играх одной лиги — дата."""
    leagues = []
    for m in matches:
        tournament = await session.get(Tournament, m.tournament_id)
        leagues.append((await session.get(League, tournament.league_id)).name)
    if len(set(leagues)) == len(leagues):
        return leagues
    return [f"{m.match_date:%d.%m}" for m in matches]


def _numbered(tags: list[str]) -> bool:
    """Длинные метки (каждая кнопка своим рядом) не влезают в 6 рядов VK — нумеруем игры («Буду 1»)."""
    return any(len(f"Не буду {t}") > PAIR_LABEL_MAX for t in tags) and len(tags) > 3


async def render_announce(session: AsyncSession, tenant_id: int, matches: list[Match]) -> str:
    """Анонс: один матч — как раньше; несколько — общий анонс недели, у каждой игры свои отметки."""
    if len(matches) == 1:
        return await render(session, matches[0], tenant_id)
    tags = await _tags(session, matches)
    numbered = _numbered(tags)
    if numbered:
        tags = [str(i + 1) for i in range(len(matches))]
    parts = ["📌 Игры недели. Отметьтесь кнопками по каждой игре, ответ можно поменять до её начала."]
    for i, m in enumerate(matches):
        head, votes = await _block(session, m, tenant_id)
        if numbered:
            head[0] = f"{i + 1}. {head[0]}"
        parts.append("\n".join([*head, *votes]))
    parts.append(
        f"Кнопка не сработала? Напишите боту в личку, например: «буду {tags[0]}» или «не буду {tags[-1]}»."
    )
    return "\n\n".join(parts)


async def announce_buttons(session: AsyncSession, matches: list[Match]) -> list[list[Button]]:
    """Кнопки анонса: один матч — «Буду / Не буду»; несколько — пара на каждую ещё не начавшуюся игру.
    VK: inline — до 10 кнопок и 6 рядов; подпись в паре — до PAIR_LABEL_MAX символов."""
    if len(matches) == 1:  # как раньше: начавшийся матч отклоняет голос в on_vote
        return vote_buttons(matches[0].id)
    now = utcnow()
    tags = await _tags(session, matches)
    if _numbered(tags):
        tags = [str(i + 1) for i in range(len(matches))]
    rows: list[list[Button]] = []
    for m, tag in zip(matches, tags, strict=True):
        if not _open_for_votes(m, now):
            continue
        going = Button(f"Буду {tag}", {"m": m.id, "v": Vote.GOING.value}, "positive")
        not_going = Button(f"Не буду {tag}", {"m": m.id, "v": Vote.NOT_GOING.value}, "negative")
        if len(not_going.label) <= PAIR_LABEL_MAX:
            rows.append([going, not_going])
        else:  # длинная метка — каждая кнопка своим рядом
            rows += [[going], [not_going]]
    return rows[:6]


async def _current_poll(session: AsyncSession, tenant_id: int, match_id: int) -> PollMessage | None:
    return (
        (
            await session.execute(
                select(PollMessage)
                .where(
                    PollMessage.tenant_id == tenant_id,
                    PollMessage.match_id == match_id,
                    PollMessage.superseded_by_id.is_(None),
                    PollMessage.closed_at.is_(None),
                )
                .order_by(PollMessage.id.desc())
            )
        )
        .scalars()
        .first()
    )


async def _announce_rows(
    session: AsyncSession, chat_id: int, message_ref: str
) -> list[tuple[PollMessage, Match]]:
    """Действующие опросы одного сообщения-анонса (общий анонс — несколько матчей), по времени игры."""
    result = await session.execute(
        select(PollMessage, Match)
        .join(Match, Match.id == PollMessage.match_id)
        .where(
            PollMessage.chat_id == chat_id,
            PollMessage.message_ref == message_ref,
            PollMessage.superseded_by_id.is_(None),
            PollMessage.closed_at.is_(None),
        )
        .order_by(Match.match_date, Match.start_at, Match.id)
    )
    return [(p, m) for p, m in result.all()]


async def _open_announce(session: AsyncSession, chat: Chat, match: Match) -> str | None:
    """Сообщение-анонс в беседе, к которому добавить матч: в нём есть ещё не начавшиеся игры той же недели."""
    rows = (
        await session.execute(
            select(PollMessage, Match)
            .join(Match, Match.id == PollMessage.match_id)
            .where(
                PollMessage.chat_id == chat.id,
                PollMessage.superseded_by_id.is_(None),
                PollMessage.closed_at.is_(None),
            )
            .order_by(PollMessage.id.desc())
        )
    ).all()
    now = utcnow()
    groups: dict[str, list[Match]] = {}
    for p, m in rows:
        groups.setdefault(p.message_ref, []).append(m)
    for ref, ms in groups.items():  # от последнего опубликованного
        same_week = all(abs((m.match_date - match.match_date).days) <= GROUP_DAYS for m in ms)
        if same_week and any(_open_for_votes(m, now) for m in ms) and len(ms) < MAX_IN_ANNOUNCE:
            return ref
    return None


async def publish(session: AsyncSession, channel: ChannelAdapter, chat: Chat, match: Match) -> PollMessage:
    """Опубликовать анонс-голосование и закрепить. Если в беседе уже есть анонс игр этой недели —
    матч добавляется в него (общий анонс), а в беседу уходит короткое сообщение. Повторный вызов
    вернёт существующий."""
    existing = await _current_poll(session, chat.tenant_id, match.id)
    if existing is not None:
        return existing
    ref = await _open_announce(session, chat, match)
    if ref is None:
        return (await _post(session, channel, chat, [match]))[0]
    session.add(PollMessage(tenant_id=chat.tenant_id, match_id=match.id, chat_id=chat.id, message_ref=ref))
    await session.commit()
    edited = await _refresh_message(session, channel, chat, ref)
    log.info("матч %s добавлен в анонс %s", match.id, ref)
    # уведомление: правка сообщения его не даёт. Одно на матч — после перезапуска опроса беседа уже знает
    if edited and await notify.once(session, chat.tenant_id, "poll_added", str(match.id), match_id=match.id):
        await session.commit()
        league, ours, rival, _ = await _context(session, match, chat.tenant_id)
        try:
            await channel.send(
                chat.peer_id,
                f"📌 В закреплённый анонс добавлена игра: {teams(match, ours, rival)} ({league.name}). "
                "Отметьтесь — «Буду» или «Не буду».",
            )
        except ChannelError as e:
            log.warning("не удалось сообщить о добавлении матча %s в анонс: %s", match.id, e)
    return await _current_poll(session, chat.tenant_id, match.id)


async def _post(
    session: AsyncSession, channel: ChannelAdapter, chat: Chat, matches: list[Match]
) -> list[PollMessage]:
    """Новое сообщение-анонс с этими матчами (закрепляется). Возвращает опросы в порядке matches."""
    text = await render_announce(session, chat.tenant_id, matches)
    ref = await channel.send(chat.peer_id, text, await announce_buttons(session, matches) or None)
    created = [
        PollMessage(tenant_id=chat.tenant_id, match_id=m.id, chat_id=chat.id, message_ref=ref.message_id)
        for m in matches
    ]
    session.add_all(created)
    for m in matches:  # сообщение о публикации и есть уведомление — «добавлена игра» для них не нужно
        await notify.once(session, chat.tenant_id, "poll_added", str(m.id), match_id=m.id)
    await session.commit()
    await channel.pin(ref)
    return created


STALE_TEXT = "Этот анонс устарел — актуальный закреплён в беседе."


async def _retire(channel: ChannelAdapter, msg: MessageRef, text: str) -> None:
    """Убрать старый анонс: удалить для всех; не вышло — пометить текстом без кнопок."""
    if not await channel.delete(msg):
        await channel.edit(msg, text)


async def _refresh_message(
    session: AsyncSession, channel: ChannelAdapter, chat: Chat, message_ref: str, force_new: bool = False
) -> bool:
    """Перерисовать сообщение-анонс; если VK не даёт отредактировать — переопубликовать все его матчи.
    force_new — сразу переопубликовать (у сообщения пропали кнопки). True — отредактировано на месте."""
    async with _msg_locks[(chat.id, message_ref)]:
        rows = await _announce_rows(session, chat.id, message_ref)
        if not rows:
            return False
        matches = [m for _, m in rows]
        text = await render_announce(session, chat.tenant_id, matches)
        buttons = await announce_buttons(session, matches) or None
        msg = MessageRef(chat.peer_id, message_ref)
        if not force_new and await channel.edit(msg, text, buttons):
            for p, _ in rows:
                p.last_edited_at = utcnow()
            await session.commit()
            return True
        new_ref = await channel.send(chat.peer_id, text, buttons)
        for p, m in rows:
            new = PollMessage(
                tenant_id=p.tenant_id, match_id=m.id, chat_id=chat.id, message_ref=new_ref.message_id
            )
            session.add(new)
            await session.flush()
            p.superseded_by_id = new.id
        await session.commit()
    await channel.pin(new_ref)
    await _retire(channel, msg, STALE_TEXT)
    return False


async def refresh(session: AsyncSession, channel: ChannelAdapter, tenant_id: int, match: Match) -> None:
    """Обновить анонс матча (весь общий анонс, если матч в нём); не редактируется — переопубликовать."""
    poll = await _current_poll(session, tenant_id, match.id)
    if poll is None:
        return
    await _refresh_message(session, channel, await session.get(Chat, poll.chat_id), poll.message_ref)


MOVED_TEXT = "Анонс перенесён в беседу команды — отмечайтесь там. Здесь голосование закрыто."


async def relocate(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, now: datetime | None = None
) -> int:
    """Переезд беседы: открытые анонсы из неактивных бесед публикуются заново в активной, старые закрываются.

    Общий анонс переезжает целиком, одним сообщением. Отметки привязаны к матчу, а не к беседе,
    поэтому в новом анонсе они сохраняются. При сбое VK анонс остаётся в старой беседе, и планировщик
    повторит попытку. Возвращает число перенесённых опросов (матчей).
    """
    chat = await chats.active_chat(session, tenant_id, channel.channel)
    if chat is None:
        return 0
    now = now or utcnow()
    rows = (
        await session.execute(
            select(PollMessage, Match, Chat)
            .join(Match, Match.id == PollMessage.match_id)
            .join(Chat, Chat.id == PollMessage.chat_id)
            .where(
                PollMessage.tenant_id == tenant_id,
                PollMessage.superseded_by_id.is_(None),
                PollMessage.closed_at.is_(None),
                Chat.channel == channel.channel,
                Chat.id != chat.id,
                Match.status.not_in(FINISHED),
                or_(Match.start_at.is_(None), Match.start_at > now),
            )
            .order_by(Match.match_date, Match.start_at, Match.id)
        )
    ).all()
    groups: dict[tuple[int, str], list] = {}
    for old, match, old_chat in rows:
        groups.setdefault((old_chat.id, old.message_ref), []).append((old, match, old_chat))
    moved = 0
    for (_, ref), items in groups.items():
        first = items[0][1]
        # попытка — не чаще раза в час: при сбое VK не долбим каждую минуту
        if not await notify.once(
            session, tenant_id, "poll_move_try", f"{first.id}:{chat.id}:{now:%Y-%m-%dT%H}", match_id=first.id
        ):
            continue
        await session.commit()
        try:
            new = await _post(session, channel, chat, [m for _, m, _ in items])
        except ChannelError as e:
            log.warning("анонс матча %s не перенесён в беседу %s: %s", first.id, chat.peer_id, e)
            continue
        by_match = {p.match_id: p for p in new}
        for old, m, _ in items:
            old.superseded_by_id = by_match[m.id].id
        await session.commit()
        moved += len(items)
        old_chat = items[0][2]
        for _, m, _ in items:
            log.info("анонс матча %s перенесён из беседы %s в %s", m.id, old_chat.peer_id, chat.peer_id)
        # без кнопок; если VK не даст отредактировать — нажатия в неактивной беседе всё равно не учитываются
        await channel.edit(MessageRef(old_chat.peer_id, ref), MOVED_TEXT)
    return moved


CLOSED_TEXT = "Время матча изменилось — голосование перезапущено. Отмечайтесь в новом анонсе, он закреплён."
CLOSED_TOAST = "Опрос перезапущен — отмечайтесь в новом анонсе"


async def _poll_open(session: AsyncSession, match: Match, now: datetime) -> bool:
    """Опрос по матчу уже открыт: есть время, матч не начался, наступил день опроса (weekly.poll_opens_at)."""
    from basket_assist.services.weekly import poll_opens_at  # weekly импортирует polls

    if match.start_at is None or match.date_is_estimate or not _open_for_votes(match, now):
        return False
    tournament = await session.get(Tournament, match.tournament_id)
    return now >= poll_opens_at(match, await session.get(League, tournament.league_id))


async def restart(session: AsyncSession, channel: ChannelAdapter, tenant_id: int, match: Match) -> list:
    """Перенос / новое время: прежние отметки сбрасываются, опрос матча закрывается на всех его
    сообщениях (нажатия на них больше не засчитываются), анонс публикуется заново.

    Новый анонс — все игры недели этого анонса (у перенесённой списки пустые, у остальных отметки
    сохраняются) внизу ленты, закрепляется; старый удаляется для всех (не вышло — пометка без кнопок).
    Если матч перенесли далеко (опрос ещё не открыт), его анонс позже опубликует недельный цикл.
    Возвращает сброшенные отметки [(player_id, vote)] — им пишут в личку.
    """
    async with _locks[match.id]:
        votes = (
            await session.execute(
                select(Attendance.player_id, Attendance.vote).filter_by(
                    tenant_id=tenant_id, match_id=match.id
                )
            )
        ).all()
        await session.execute(delete(Attendance).filter_by(tenant_id=tenant_id, match_id=match.id))
        current = await _current_poll(session, tenant_id, match.id)
        siblings = []
        if current is not None:
            siblings = [
                (p, m)
                for p, m in await _announce_rows(session, current.chat_id, current.message_ref)
                if m.id != match.id
            ]
        now = utcnow()
        for p in (
            await session.scalars(
                select(PollMessage).where(
                    PollMessage.tenant_id == tenant_id,
                    PollMessage.match_id == match.id,
                    PollMessage.closed_at.is_(None),
                )
            )
        ).all():
            p.closed_at = now
        await session.commit()
    result = [(player_id, Vote(vote)) for player_id, vote in votes]
    if current is None:
        return result
    old_chat = await session.get(Chat, current.chat_id)
    old_msg = MessageRef(old_chat.peer_id, current.message_ref)
    chat = await chats.active_chat(session, tenant_id, channel.channel) or old_chat
    matches = [m for _, m in siblings]
    same_week = all(abs((m.match_date - match.match_date).days) <= GROUP_DAYS for m in matches)
    if same_week and await _poll_open(session, match, now):  # иначе — отдельный анонс недельным циклом
        matches.append(match)
    matches.sort(key=lambda m: (m.match_date, m.start_at or datetime.max, m.id))
    if matches:
        try:
            new = await _post(session, channel, chat, matches)
        except ChannelError as e:
            log.warning("анонс после перезапуска матча %s не опубликован: %s", match.id, e)
            if siblings:  # убрать матч из старого анонса; вернёт его недельный цикл
                await _refresh_message(session, channel, old_chat, current.message_ref)
            else:
                await channel.edit(old_msg, CLOSED_TEXT)
            return result
        by_match = {p.match_id: p for p in new}
        for p, m in siblings:
            p.superseded_by_id = by_match[m.id].id
        await session.commit()
    await _retire(channel, old_msg, CLOSED_TEXT)
    log.info("опрос по матчу %s перезапущен: сброшено отметок %s", match.id, len(votes))
    return result


async def _closed_poll(session: AsyncSession, msg: MessageRef, match_id: int) -> bool:
    """Нажатие пришло с сообщения, где опрос этого матча закрыт (перезапущен после переноса)."""
    found = (
        await session.scalars(
            select(PollMessage)
            .join(Chat, Chat.id == PollMessage.chat_id)
            .where(
                Chat.peer_id == msg.peer_id,
                PollMessage.message_ref == msg.message_id,
                PollMessage.match_id == match_id,
            )
        )
    ).all()
    return bool(found) and all(p.closed_at is not None for p in found)


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
    if await _closed_poll(session, press.message, match_id):
        await channel.answer_button(press, CLOSED_TOAST)
        return

    player, _ = await get_or_create_player(session, tenant_id, channel, press.user_id)
    await _apply_vote(
        session,
        channel,
        tenant_id,
        match,
        player,
        vote,
        lambda: channel.answer_button(press, f"Отмечено: {VOTE_LABELS[vote]}"),
    )


async def _apply_vote(
    session: AsyncSession,
    channel: ChannelAdapter,
    tenant_id: int,
    match: Match,
    player: Player,
    vote: Vote,
    before_refresh=None,
) -> bool:
    """Записать отметку (кнопкой или текстом) и всё, что за ней следует. True — отметка изменилась.
    before_refresh — что сделать сразу после записи (ответ на нажатие, пока анонс перерисовывается)."""
    async with _locks[match.id]:
        row = (
            await session.execute(
                select(Attendance).filter_by(tenant_id=tenant_id, match_id=match.id, player_id=player.id)
            )
        ).scalar_one_or_none()
        changed = row is None or row.vote != vote
        if row is None:
            session.add(Attendance(tenant_id=tenant_id, match_id=match.id, player_id=player.id, vote=vote))
        elif changed:
            row.vote, row.updated_at = vote, utcnow()
        await session.commit()
        if before_refresh is not None:
            await before_refresh()
        if changed:  # повторное нажатие той же кнопки ничего не меняет (domain.md)
            await refresh(session, channel, tenant_id, match)
    if changed:
        from basket_assist.services import weekly  # weekly импортирует polls

        await weekly.vote_changed(session, channel, tenant_id, match, player, VOTE_LABELS[vote])
    if changed and vote == Vote.GOING:
        await _signal_not_in_roster(session, channel, tenant_id, match, player)
    return changed


TEXT_VOTE = re.compile(r"^(не\s*буду|буду)(?:\s+(?:на\s+)?(.+))?$")


async def _votable(session: AsyncSession, tenant_id: int, channel: ChannelAdapter) -> list[tuple[Match, int]]:
    """Игры с открытым голосованием в беседе команды — по времени игры, с номером игры в её анонсе
    (как в нумерованном анонсе «1.», «2.»: считаются все игры сообщения, и уже начавшиеся тоже)."""
    chat = await chats.active_chat(session, tenant_id, channel.channel)
    if chat is None:
        return []
    now = utcnow()
    refs = (
        await session.scalars(
            select(PollMessage.message_ref)
            .where(
                PollMessage.chat_id == chat.id,
                PollMessage.superseded_by_id.is_(None),
                PollMessage.closed_at.is_(None),
            )
            .distinct()
        )
    ).all()
    out = []
    for ref in refs:
        for i, (_, m) in enumerate(await _announce_rows(session, chat.id, ref)):
            if _open_for_votes(m, now):
                out.append((m, i + 1))
    out.sort(key=lambda x: (x[0].match_date, x[0].start_at or datetime.min, x[0].id))
    return out


async def text_vote(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, player: Player, text: str
) -> str | None:
    """Отметка текстом в личке («буду», «не буду МЛБЛ», «буду 25.10») — запасной путь, если кнопки
    анонса не работают. None — это не отметка. Администратору сервиса — сигнал (раз на игрока и матч)."""
    m = TEXT_VOTE.match(" ".join(text.lower().replace("ё", "е").strip(" .!?").split()))
    if m is None:
        return None
    vote = Vote.GOING if m.group(1) == "буду" else Vote.NOT_GOING
    word = VOTE_LABELS[vote].lower()
    votable = await _votable(session, tenant_id, channel)
    if not votable:
        return "Сейчас нет игр, на которые открыто голосование."
    matches = [mt for mt, _ in votable]
    tags = await _tags(session, matches)  # как на кнопках: лига, при двух играх одной лиги — дата
    variants = f"Варианты: {', '.join(tags)}."
    if (query := m.group(2)) is None:
        if len(matches) > 1:
            return f"Игр несколько — уточните, например: «{word} {tags[0]}». {variants}"
        match = matches[0]
    else:
        found = [
            mt
            for (mt, number), tag in zip(votable, tags, strict=True)
            if query in (tag.lower(), str(number), f"{mt.match_date:%d.%m}")
        ]
        if len(found) != 1:
            return f"Не нашёл игру «{query}». {variants}"
        match = found[0]
    await _apply_vote(session, channel, tenant_id, match, player, vote)
    league, ours, rival, _ = await _context(session, match, tenant_id)
    what = f"{league.name}, {teams(match, ours, rival)}, {match.match_date:%d.%m}"
    if await notify.once(
        session, tenant_id, "text_vote", f"{match.id}:{player.id}", match_id=match.id, player_id=player.id
    ):
        await session.commit()
        await notify.admin(
            channel,
            f"ℹ️ {player.display_name} отметился текстом в личке: «{VOTE_LABELS[vote]}» ({what}). "
            "Если так делают несколько игроков — возможно, не работают кнопки анонса.",
        )
    return f"Отмечено: {VOTE_LABELS[vote]} — {what}."


async def check_announces(session: AsyncSession, channel: ChannelAdapter) -> int:
    """Самопроверка (раз в час): у действующих анонсов есть кнопки, последний из них закреплён.

    Кнопки пропали (24.10: VK потерял их у закрепа, нажатия давали «Не удалось выполнить запрос») —
    анонс публикуется заново. Закреплено другое — закрепляем анонс снова, но один раз на сообщение:
    если участник закрепит своё ещё раз, не спорим. Об обоих случаях — сообщение администратору сервиса.
    Возвращает число исправленных анонсов.
    """
    now = utcnow()
    rows = (
        await session.execute(
            select(PollMessage, Match, Chat)
            .join(Match, Match.id == PollMessage.match_id)
            .join(Chat, Chat.id == PollMessage.chat_id)
            .where(
                Chat.active.is_(True),
                PollMessage.superseded_by_id.is_(None),
                PollMessage.closed_at.is_(None),
            )
            .order_by(PollMessage.id)
        )
    ).all()
    by_msg: dict[tuple[int, str], tuple[Chat, list[Match]]] = {}
    for poll, match, chat in rows:  # от старых к новым: последний в беседе — тот, что должен быть закреплён
        by_msg.setdefault((chat.id, poll.message_ref), (chat, []))[1].append(match)
    latest = {chat_id: ref for chat_id, ref in by_msg}
    fixed = 0
    for (chat_id, ref), (chat, matches) in by_msg.items():
        if not any(_open_for_votes(m, now) for m in matches):
            continue  # голосование закончилось — кнопок и не должно быть
        msg = MessageRef(chat.peer_id, ref)
        if await channel.has_buttons(msg) is False:
            log.warning("у анонса %s нет кнопок — публикую заново", msg)
            await _refresh_message(session, channel, chat, ref, force_new=True)
            await notify.admin(
                channel,
                "⚠️ У закреплённого анонса пропали кнопки «Буду / Не буду» — опубликовал анонс заново. "
                "Отметки сохранены, но нажатия за последний час могли не дойти.",
            )
            fixed += 1
            continue
        if latest[chat_id] != ref:
            continue
        pinned = await channel.pinned(chat.peer_id)
        if pinned is None or pinned == ref:  # None — не удалось узнать
            continue
        if await notify.was_sent(session, chat.tenant_id, "repin", f"{chat_id}:{ref}"):
            continue  # уже перезакрепляли это сообщение — закреп сменили намеренно
        if await channel.pin(msg):  # не вышло — попробуем через час
            await notify.once(session, chat.tenant_id, "repin", f"{chat_id}:{ref}")
            await session.commit()
            log.warning("анонс %s был откреплён — закрепил снова", msg)
            await notify.admin(channel, "ℹ️ Анонс игр недели был откреплён — закрепил его снова.")
            fixed += 1
    return fixed


async def _signal_not_in_roster(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, match: Match, player: Player
) -> None:
    """«Буду», но игрока нет в заявке на сезон этой лиги — сигнал игроку и руководителям (один раз на матч)."""
    league, ours, rival, entry = await _context(session, match, tenant_id)
    status = await roster.roster_status(session, tenant_id, player.id, entry.id)
    if status == "in_roster":
        return
    if not await notify.once(
        session, tenant_id, "not_in_roster", f"{match.id}:{player.id}", match_id=match.id, player_id=player.id
    ):
        return
    await session.commit()
    match_text = f"{teams(match, ours, rival)} ({league.name})"
    if status == "not_in_league":
        to_player = (
            f"Вы отметились «Буду» на матч {match_text}, но у вас отметка «не играет в этой лиге». "
            "Если хотите играть — обратитесь к капитану."
        )
        to_leader = (
            f"⚠️ {player.display_name} отметился «Буду» на {match_text}, но отмечен «не играет в этой лиге». "
            f"Если он будет играть — дозаявите и исправьте отметку («исправить {player.display_name}»)."
        )
    else:
        what = "нет в заявке на сезон" if status == "not_in_roster" else "не сопоставлен с заявкой на сезон"
        to_player = (
            f"Вы отметились «Буду» на матч {match_text}, но вас {what}. "
            "Без заявки на сезон сыграть не получится — обратитесь к капитану."
        )
        to_leader = (
            f"⚠️ {player.display_name} отметился «Буду» на {match_text}, "
            f"но {what}. Нужно дозаявить или сопоставить («сопоставление»)."
        )
    await notify.dm(session, channel, tenant_id, player.id, to_player)
    for lead in await leaders.leaders(session, tenant_id):
        if lead.id != player.id:
            await notify.dm(session, channel, tenant_id, lead.id, to_leader)
