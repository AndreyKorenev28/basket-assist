"""Состав на матч для руководителей команды: кто отметился на ближайшие игры (domain.md, «Состав на матч»).

Сейчас — по запросу (команда «состав»). Тот же текст станет основой сводок за 24 ч и 2 ч (M4).
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.domain.models import Vote
from basket_assist.services import polls, roster
from basket_assist.services.schedule import format_when, kit_hint, teams, upcoming_matches
from basket_assist.storage.tables import Attendance, League, LeagueTeam, MessengerAccount, Player, TeamEntry

NOT_ELIGIBLE = {
    "not_in_roster": "нет в заявке на сезон",
    "not_in_league": "не играет в этой лиге",
    "unmatched": "не сопоставлен с заявкой на сезон",
}


async def lineup_text(session: AsyncSession, tenant_id: int) -> str:
    """Состав на ближайший матч каждой лиги на текущий момент."""
    nearest: dict[int, tuple] = {}  # выступление → первый предстоящий матч
    for league, ours, rival, match in await upcoming_matches(session, tenant_id):
        entry = (await polls._context(session, match, tenant_id))[3]
        nearest.setdefault(entry.id, (league, ours, rival, match))
    entries = (
        await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id).order_by(TeamEntry.id))
    ).all()
    blocks, tz = [], None
    for entry in entries:  # каждая лига — даже без матчей, чтобы капитан не гадал, куда она делась
        if entry.id in nearest:
            league = nearest[entry.id][0]
            blocks.append(await _match_block(session, tenant_id, *nearest[entry.id]))
        else:
            ours = await session.get(LeagueTeam, entry.league_team_id)
            league = await session.get(League, ours.league_id)
            blocks.append(f"🏀 {league.name}: {ours.name}\nБлижайших матчей на сайте лиги пока нет.")
        tz = tz or ZoneInfo(league.timezone)  # в MVP у всех лиг Europe/Moscow
    if not blocks:
        return "Ближайших матчей на сайтах лиг пока нет."
    now = datetime.now(UTC).astimezone(tz)
    return "\n\n".join(blocks) + f"\n\nНа {now:%d.%m %H:%M}"


async def _match_block(session: AsyncSession, tenant_id: int, league, ours, rival, match) -> str:
    _, _, _, entry = await polls._context(session, match, tenant_id)
    head = [
        f"🏀 {league.name}: {teams(match, ours, rival)}",
        f"📅 {format_when(match, league)}",
        kit_hint(match, ours),
    ]
    if match.start_at is None:
        return "\n".join([*head, "Время не назначено — опрос не запускается, свяжитесь с лигой."])
    if await polls._current_poll(session, tenant_id, match.id) is None:
        return "\n".join([*head, "Опрос ещё не запущен."])

    dm_open = set(
        (
            await session.scalars(
                select(MessengerAccount.player_id).filter_by(tenant_id=tenant_id, dm_allowed=True)
            )
        ).all()
    )
    votes = dict(
        (
            await session.execute(
                select(Attendance.player_id, Attendance.vote).filter_by(
                    tenant_id=tenant_id, match_id=match.id
                )
            )
        ).all()
    )
    players = (await session.scalars(select(Player).filter_by(tenant_id=tenant_id))).all()

    going_ok, going_bad, not_going, silent = [], [], [], []
    for p in players:
        status = await roster.roster_status(session, tenant_id, p.id, entry.id)
        name = await polls.player_name(session, tenant_id, p, entry)
        vote = votes.get(p.id)
        if vote == Vote.GOING:
            if status == "in_roster":
                going_ok.append((name, await _number(session, tenant_id, p.id, entry)))
            else:
                going_bad.append(f"⚠️ {name} — {NOT_ELIGIBLE[status]}")
        elif vote == Vote.NOT_GOING:
            not_going.append(name)
        elif p.active and status == "in_roster":  # ждём ответа только от заявленных в этой лиге
            silent.append(name + ("" if p.id in dm_open else " 🔕"))

    going_ok.sort(key=lambda t: _sort_key(t[0]))
    lines = [*head, ""]
    lines.append(f"Буду ({len(going_ok) + len(going_bad)}):")
    lines += [f"  {'№' + num if num else '№ ?'} {name}" for name, num in going_ok]
    lines += [f"  {line}" for line in sorted(going_bad, key=_sort_key)]
    lines.append(_size_note(len(going_ok), league.min_players, league.max_players))
    lines.append(f"Не буду ({len(not_going)}): " + (", ".join(sorted(not_going, key=_sort_key)) or "—"))
    lines.append(f"Не ответили ({len(silent)}): " + (", ".join(sorted(silent, key=_sort_key)) or "—"))
    return "\n".join(lines)


def _size_note(n: int, lo: int, hi: int) -> str:
    """Проверка размера заявки на игру: в неё идут только заявленные на сезон."""
    note = f"В заявку на игру: {n} (нужно {lo}–{hi})"
    if n < lo:
        return f"{note} — ⚠️ не хватает {lo - n}"
    if n > hi:
        return f"{note} — ⚠️ лишних {n - hi}"
    return f"{note} ✅"


async def _number(session: AsyncSession, tenant_id: int, player_id: int, entry: TeamEntry) -> str:
    """Номер для заявки на игру: ручная правка руководителя важнее номера из заявки на сезон."""
    link = await roster._link(session, tenant_id, player_id, entry.id)
    if link is not None and link.number_override:
        return str(link.number_override)
    r = await roster.roster_entry_for(session, tenant_id, player_id, entry.id)
    return str(r.number) if r is not None and r.number else ""


def _sort_key(text: str) -> str:
    return text.removeprefix("⚠️ ").lower().replace("ё", "е")
