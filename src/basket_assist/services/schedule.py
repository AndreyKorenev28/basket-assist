"""Ближайшие матчи команды сервиса — для «Игры» в боте и --show."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.storage.tables import League, LeagueTeam, Match, TeamEntry

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


async def upcoming_matches(session: AsyncSession, tenant_id: int, limit: int = 3) -> list[tuple]:
    """[(league, ours, rival, match)] — ближайшие матчи по всем выступлениям, по дате."""
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
                .order_by(Match.match_date)
                .limit(limit)
            )
        ).all()
        for m in matches:
            if m.start_at and m.start_at < datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=3):
                continue  # уже сыгран
            rival = await session.get(
                LeagueTeam, m.away_team_id if m.home_team_id == ours.id else m.home_team_id
            )
            out.append((league, ours, rival, m))
    return sorted(out, key=lambda x: (x[3].match_date, x[3].start_at or datetime.max))


def format_when(match: Match, league: League) -> str:
    if match.start_at:
        local = match.start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(league.timezone))
        return f"{WEEKDAYS[local.weekday()]} {local:%d.%m} {local:%H:%M}"
    return f"{WEEKDAYS[match.match_date.weekday()]} {match.match_date:%d.%m} (время не назначено)"


async def upcoming_text(session: AsyncSession, tenant_id: int) -> str:
    items = await upcoming_matches(session, tenant_id)
    if not items:
        return "Ближайших матчей на сайтах лиг пока нет."
    lines = ["Ближайшие игры:"]
    for league, ours, rival, m in items:
        lines.append(
            f"• {format_when(m, league)} — {league.name}: {ours.name} — {rival.name}; {m.venue_name or ''}"
        )
    return "\n".join(lines)
