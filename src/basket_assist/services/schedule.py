"""Ближайшие матчи команды сервиса — для «Игры» в боте и --show."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.domain.models import MatchStatus
from basket_assist.storage.tables import League, LeagueTeam, Match, Standing, TeamEntry

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
# Сколько после начала матч ещё считается текущим. Результат на сайте лиги появляется с задержкой,
# а синхронизации по расписанию пока нет (M4), поэтому уже начавшуюся игру убираем по времени.
MATCH_DURATION = timedelta(hours=2)
FINISHED = (MatchStatus.PLAYED, MatchStatus.FORFEIT, MatchStatus.CANCELLED)  # в «Игры» не показываем


async def upcoming_matches(
    session: AsyncSession, tenant_id: int, limit: int = 3, include_estimate: bool = False
) -> list[tuple]:
    """[(league, ours, rival, match)] — ближайшие матчи по всем выступлениям, по дате.

    Черновики без даты (date_is_estimate) не показываются: пока лига не назначила дату, матча для команды нет
    (решение владельца 2026-10-19). include_estimate=True — только для ответа руководителю («время …»).
    """
    out = []
    entries = (await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id))).all()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    for entry in entries:
        ours = await session.get(LeagueTeam, entry.league_team_id)
        league = await session.get(League, ours.league_id)
        matches = (
            await session.scalars(
                select(Match)
                .where(Match.tournament_id == entry.tournament_id)
                .where(or_(Match.home_team_id == ours.id, Match.away_team_id == ours.id))
                .where(Match.match_date >= yesterday)
                .where(Match.status.not_in(FINISHED))  # до limit, чтобы сыгранные не вытеснили будущие
                .where(true() if include_estimate else Match.date_is_estimate.is_(False))
                .order_by(Match.match_date)
                .limit(limit)
            )
        ).all()
        now = datetime.now(UTC).replace(tzinfo=None)
        for m in matches:
            if m.status in FINISHED or (m.start_at and m.start_at <= now - MATCH_DURATION):
                continue  # уже сыгран
            rival = await session.get(
                LeagueTeam, m.away_team_id if m.home_team_id == ours.id else m.home_team_id
            )
            out.append((league, ours, rival, m))
    return sorted(out, key=lambda x: (x[3].match_date, x[3].start_at or datetime.max))


def teams(match: Match, ours: LeagueTeam, rival: LeagueTeam) -> str:
    """Команды в порядке календаря лиги: слева хозяин (в API — home), как на сайте."""
    home, away = (ours, rival) if match.home_team_id == ours.id else (rival, ours)
    return f"{home.name} — {away.name}"


def kit_hint(match: Match, ours: LeagueTeam) -> str:
    """Цвет формы: у команды слева в календаре — светлая, справа — тёмная (domain.md, «Цвет формы»)."""
    # цвет кружка — цвет формы: футболки нужного цвета среди эмодзи нет (решение владельца 2026-10-21)
    return "⚪ Наша форма: светлая" if match.home_team_id == ours.id else "🟣 Наша форма: тёмная"


def weekend_days(match: Match) -> tuple:
    """Ожидаемые выходные для матча без опубликованной даты: (суббота, воскресенье)."""
    sat = match.match_date
    return sat, sat + timedelta(days=1)


def format_when(match: Match, league: League) -> str:
    if match.date_is_estimate and not match.start_at:
        sat, sun = weekend_days(match)
        days = f"{sat:%d}–{sun:%d.%m}" if sat.month == sun.month else f"{sat:%d.%m}–{sun:%d.%m}"
        return f"выходные {days}, дата и время пока не назначены"
    if match.start_at:
        local = match.start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(league.timezone))
        return f"{WEEKDAYS[local.weekday()]} {local:%d.%m}, {local:%H:%M}"
    return f"{WEEKDAYS[match.match_date.weekday()]} {match.match_date:%d.%m}, время пока не назначено"


def _record(st: Standing) -> str:
    """«6-е, 3–4 (−12)»: место, победы–поражения, разница забитых и пропущенных."""
    text = f"{st.place}-е, {st.wins}–{st.losses}" if st.place else f"{st.wins}–{st.losses}"
    if st.points_for is not None and st.points_against is not None:
        diff = st.points_for - st.points_against
        text += f" ({'+' if diff > 0 else '−' if diff < 0 else '±'}{abs(diff)})"
    return text


async def rival_lines(session: AsyncSession, match: Match, ours: LeagueTeam, rival: LeagueTeam) -> list[str]:
    """Справка по сопернику для «Игр»: таблица лиги и очные встречи сезона — всё с нашей стороны."""
    standings = {
        st.league_team_id: st
        for st in (
            await session.scalars(
                select(Standing).where(
                    Standing.tournament_id == match.tournament_id,
                    Standing.league_team_id.in_([ours.id, rival.id]),
                )
            )
        ).all()
    }
    if ours.id in standings and rival.id in standings:
        lines = [f"Мы: {_record(standings[ours.id])}. Они: {_record(standings[rival.id])}"]
    else:
        lines = ["Таблица лиги пока не опубликована"]
    played = (
        await session.scalars(
            select(Match)
            .where(
                Match.tournament_id == match.tournament_id,
                or_(
                    (Match.home_team_id == ours.id) & (Match.away_team_id == rival.id),
                    (Match.home_team_id == rival.id) & (Match.away_team_id == ours.id),
                ),
                Match.score_home.is_not(None),
                Match.score_away.is_not(None),
            )
            .order_by(Match.match_date)
        )
    ).all()
    if not played:
        lines.append("Мы vs они: ещё не играли")
        return lines
    scores = [
        (m.score_home, m.score_away) if m.home_team_id == ours.id else (m.score_away, m.score_home)
        for m in played
    ]
    won = sum(1 for we, they in scores if we > they)
    lost = sum(1 for we, they in scores if we < they)
    lines.append(f"Мы vs они: {won}–{lost} ({', '.join(f'{we}:{they}' for we, they in scores)})")
    return lines


async def upcoming_text(session: AsyncSession, tenant_id: int) -> str:
    """«Игры»: по одной ближайшей игре в каждой лиге; лига без матчей тоже в списке."""
    nearest: dict[int, tuple] = {}  # наша команда в лиге → её первый предстоящий матч
    for item in await upcoming_matches(session, tenant_id):
        nearest.setdefault(item[1].id, item)
    entries = (
        await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id).order_by(TeamEntry.id))
    ).all()
    blocks = []
    for entry in entries:
        if entry.league_team_id in nearest:
            league, ours, rival, m = nearest[entry.league_team_id]
            place = ", ".join(x for x in (m.venue_name, m.venue_address) if x) or "зал пока не указан"
            blocks.append(
                f"🏀 {league.name}: {teams(m, ours, rival)}\n📅 {format_when(m, league)}\n{place}\n"
                f"{kit_hint(m, ours)}\n" + "\n".join(await rival_lines(session, m, ours, rival))
            )
        else:
            ours = await session.get(LeagueTeam, entry.league_team_id)
            league = await session.get(League, ours.league_id)
            blocks.append(f"🏀 {league.name}: {ours.name}\nБлижайших матчей на сайте лиги пока нет.")
    if not blocks:
        return "Ближайших матчей на сайтах лиг пока нет."
    return "Ближайшие игры:\n\n" + "\n\n".join(blocks)
