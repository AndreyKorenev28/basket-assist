"""Состав на матч для руководителей команды: кто отметился на ближайшие игры (domain.md, «Состав на матч»).

По запросу (команда «состав») и в сводках за 24 ч и 2 ч до матча (`match_digest`, services/weekly.py).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.domain.models import Vote
from basket_assist.services import polls, roster
from basket_assist.services.schedule import format_when, kit_hint, teams, upcoming_matches
from basket_assist.storage.tables import (
    ApplicationEntry,
    Attendance,
    League,
    LeagueTeam,
    Match,
    MessengerAccount,
    Player,
    TeamEntry,
)

NEW = "🆕 "  # отметка изменений с прошлой сводки (финальная за 2 ч)
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


async def _match_block(
    session: AsyncSession, tenant_id: int, league, ours, rival, match, since: datetime | None = None
) -> str:
    """since — время прошлой сводки: изменившиеся после неё ответы и правки отмечены NEW."""
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

    app = await application(session, tenant_id, match)
    in_app = {row.player_id for row in app}
    removed = await _removed_ids(session, tenant_id, match.id)

    changed = await _changed_since(session, tenant_id, match.id, since) if since else set()

    def mark(pid: int) -> str:
        return NEW if pid in changed else ""

    going_bad, going_removed, not_going, silent = [], [], [], []
    for p in players:
        if p.id in in_app:
            continue  # в заявке на игру — выводится ниже из `app`
        status = await roster.roster_status(session, tenant_id, p.id, entry.id)
        name = await polls.player_name(session, tenant_id, p, entry)
        new = mark(p.id)  # 🆕 всегда в начале строки
        vote = votes.get(p.id)
        if vote == Vote.GOING:
            if p.id in removed:
                going_removed.append(f"{new}✖️ {name} — убран из заявки руководителем")
            else:
                going_bad.append(f"{new}⚠️ {name} — {NOT_ELIGIBLE[status]}")
        elif vote == Vote.NOT_GOING:
            not_going.append(new + name)
        elif p.active and status == "in_roster":  # ждём ответа только от заявленных в этой лиге
            silent.append(name + ("" if p.id in dm_open else " 🔕"))

    voted = [row for row in app if not row.added]
    added = [row for row in app if row.added]
    lines = [*head, ""]
    if since is not None:
        lines += [
            f"Изменилось с прошлой сводки: {len(changed)}"
            + (f" (отмечены {NEW.strip()})" if changed else ""),
            "",
        ]
    lines.append(f"Буду ({len(voted) + len(going_bad) + len(going_removed)}):")
    lines += [f"  {mark(row.player_id)}{row.line_with_sign()}" for row in voted]
    lines += [f"  {line}" for line in sorted(going_bad + going_removed, key=_sort_key)]
    if added:
        lines.append(f"Добавлены руководителем ({len(added)}):")
        lines += [f"  {mark(row.player_id)}{row.line_with_sign()}" for row in added]
    lines.append(_size_note(len(app), league.min_players, league.max_players))
    lines.append(f"Не буду ({len(not_going)}): " + (", ".join(sorted(not_going, key=_sort_key)) or "—"))
    lines.append(f"Не ответили ({len(silent)}): " + (", ".join(sorted(silent, key=_sort_key)) or "—"))
    return "\n".join(lines)


async def match_digest(
    session: AsyncSession, tenant_id: int, match: Match, title: str, since: datetime | None = None
) -> str:
    """Сводка руководителю по одному матчу (за 24 ч / за 2 ч): заголовок + состав на матч."""
    league, ours, rival, _ = await polls._context(session, match, tenant_id)
    now = datetime.now(UTC).astimezone(ZoneInfo(league.timezone))
    block = await _match_block(session, tenant_id, league, ours, rival, match, since)
    return f"{title}\n\n{block}\n\nНа {now:%d.%m %H:%M}"


@dataclass
class AppRow:
    """Строка заявки на игру."""

    player_id: int
    name: str  # ФИО из заявки на сезон
    number: str  # "" — номера нет
    added: bool  # добавлен руководителем (а не по голосу «Буду»)

    def line(self) -> str:
        return f"{self.number or '?'} {self.name}"

    def line_with_sign(self) -> str:
        return f"{'№' + self.number if self.number else '№ ?'} {self.name}"


async def application(session: AsyncSession, tenant_id: int, match: Match) -> list[AppRow]:
    """Заявка на игру сейчас: «Буду» среди заявленных на сезон + правки руководителя (domain.md).

    Правка руководителя сильнее голоса: убранный не вернётся, даже если снова нажмёт «Буду»;
    добавленный остаётся, даже если ответил «Не буду». В заявку идут только заявленные на сезон.
    """
    _, _, _, entry = await polls._context(session, match, tenant_id)
    going = set(
        (
            await session.scalars(
                select(Attendance.player_id).filter_by(
                    tenant_id=tenant_id, match_id=match.id, vote=Vote.GOING
                )
            )
        ).all()
    )
    edits = {
        e.player_id: e
        for e in (
            await session.scalars(select(ApplicationEntry).filter_by(tenant_id=tenant_id, match_id=match.id))
        ).all()
    }
    ids = {pid for pid in going if not (pid in edits and edits[pid].removed_by_captain)}
    ids |= {pid for pid, e in edits.items() if not e.removed_by_captain}
    rows = []
    for pid in ids:
        if await roster.roster_status(session, tenant_id, pid, entry.id) != "in_roster":
            continue
        player = await session.get(Player, pid)
        name = await polls.player_name(session, tenant_id, player, entry)
        number = await _number(session, tenant_id, pid, entry)
        rows.append(AppRow(pid, name, number, added=pid not in going))
    return sorted(rows, key=lambda r: (_sort_key(r.name), r.player_id))


async def _removed_ids(session: AsyncSession, tenant_id: int, match_id: int) -> set[int]:
    return set(
        (
            await session.scalars(
                select(ApplicationEntry.player_id).filter_by(
                    tenant_id=tenant_id, match_id=match_id, removed_by_captain=True
                )
            )
        ).all()
    )


async def application_text(session: AsyncSession, tenant_id: int, match: Match) -> str | None:
    """Заявка на игру для копирования: «номер ФИО» построчно. None — некого подавать.

    Отдельным сообщением: в VK на телефоне сообщение копируется только целиком.
    """
    return "\n".join(row.line() for row in await application(session, tenant_id, match)) or None


async def _changed_since(session: AsyncSession, tenant_id: int, match_id: int, since: datetime) -> set[int]:
    """Игроки, чей ответ или место в заявке на игру изменились после `since` (UTC без tzinfo)."""
    voted = await session.scalars(
        select(Attendance.player_id).where(
            Attendance.tenant_id == tenant_id, Attendance.match_id == match_id, Attendance.updated_at > since
        )
    )
    edited = await session.scalars(
        select(ApplicationEntry.player_id).where(
            ApplicationEntry.tenant_id == tenant_id,
            ApplicationEntry.match_id == match_id,
            ApplicationEntry.updated_at > since,
        )
    )
    return set(voted.all()) | set(edited.all())


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
    return (
        text.removeprefix(NEW)
        .removeprefix("⚠️ ")
        .removeprefix("✖️ ")
        .removeprefix(NEW)
        .lower()
        .replace("ё", "е")
    )
