"""Сопоставление игроков с заявками на сезон (domain.md, «Сопоставление игрока с заявкой на сезон»).

Решения принимает только руководитель команды; бот предлагает кандидатов (domain/matching.py).
Связь устанавливается отдельно для каждого выступления (лиги).
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.domain.matching import Candidate, Confidence, RosterName, norm, suggest
from basket_assist.domain.models import Channel, RosterLinkStatus
from basket_assist.storage.tables import (
    League,
    LeagueTeam,
    MessengerAccount,
    Player,
    RosterLink,
    SeasonRosterEntry,
    TeamEntry,
    utcnow,
)

DONE = (RosterLinkStatus.CONFIRMED, RosterLinkStatus.NOT_IN_ROSTER)


class MatchConflict(Exception):
    """Строка заявки на сезон уже связана с другим игроком."""


@dataclass
class QueueItem:
    player: Player
    entry: TeamEntry
    league_name: str
    candidates: list[Candidate]  # ключи — id SeasonRosterEntry


def split_name(display_name: str) -> tuple[str, str]:
    first, _, last = display_name.strip().partition(" ")
    return first, last


async def entry_label(session: AsyncSession, entry: TeamEntry) -> str:
    team = await session.get(LeagueTeam, entry.league_team_id)
    league = await session.get(League, team.league_id)
    return f"{league.name} ({team.name})"


async def roster_rows(session: AsyncSession, entry: TeamEntry) -> list[SeasonRosterEntry]:
    return list(
        (
            await session.scalars(
                select(SeasonRosterEntry)
                .filter_by(
                    league_team_id=entry.league_team_id, tournament_id=entry.tournament_id, active=True
                )
                .order_by(SeasonRosterEntry.last_name, SeasonRosterEntry.first_name)
            )
        ).all()
    )


async def _link(session: AsyncSession, tenant_id: int, player_id: int, entry_id: int) -> RosterLink | None:
    return (
        await session.execute(
            select(RosterLink).filter_by(tenant_id=tenant_id, player_id=player_id, team_entry_id=entry_id)
        )
    ).scalar_one_or_none()


async def _taken(session: AsyncSession, tenant_id: int, entry: TeamEntry) -> dict[int, int]:
    """{season_roster_entry_id: player_id} — уже подтверждённые строки выступления."""
    rows = (
        await session.execute(
            select(RosterLink.season_roster_entry_id, RosterLink.player_id).where(
                RosterLink.tenant_id == tenant_id,
                RosterLink.team_entry_id == entry.id,
                RosterLink.status == RosterLinkStatus.CONFIRMED,
            )
        )
    ).all()
    return {r: p for r, p in rows if r is not None}


async def candidates(
    session: AsyncSession, tenant_id: int, player: Player, entry: TeamEntry
) -> list[Candidate]:
    taken = await _taken(session, tenant_id, entry)
    rows = [r for r in await roster_rows(session, entry) if r.id not in taken]
    first, last = split_name(player.display_name)
    found = suggest(first, last, [RosterName(r.id, r.last_name, r.first_name) for r in rows])
    if found:
        return found
    # Игрок уже связан в другой лиге — сравниваем две официальные записи (ФИО из заявок на сезон)
    other = (
        (
            await session.execute(
                select(SeasonRosterEntry)
                .join(RosterLink, RosterLink.season_roster_entry_id == SeasonRosterEntry.id)
                .where(
                    RosterLink.tenant_id == tenant_id,
                    RosterLink.player_id == player.id,
                    RosterLink.status == RosterLinkStatus.CONFIRMED,
                    RosterLink.team_entry_id != entry.id,
                )
            )
        )
        .scalars()
        .first()
    )
    if other is None:
        return []
    return [
        Candidate(r.id, Confidence.EXACT)
        for r in rows
        if norm(r.last_name) == norm(other.last_name)
        and norm(r.first_name) == norm(other.first_name)
        and (r.birth_date is None or other.birth_date is None or r.birth_date == other.birth_date)
    ]


async def queue(session: AsyncSession, tenant_id: int) -> list[QueueItem]:
    """Кто ещё не сопоставлен — по каждому выступлению. Игроки «не игрок команды» (active=False) исключены."""
    entries = (
        await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id).order_by(TeamEntry.id))
    ).all()
    players = (
        await session.scalars(
            select(Player).filter_by(tenant_id=tenant_id, active=True).order_by(Player.display_name)
        )
    ).all()
    out = []
    for entry in entries:
        label = await entry_label(session, entry)
        for p in players:
            link = await _link(session, tenant_id, p.id, entry.id)
            if link is not None and link.status in DONE:
                continue
            out.append(QueueItem(p, entry, label, await candidates(session, tenant_id, p, entry)))
    return out


async def _set_link(
    session: AsyncSession,
    tenant_id: int,
    player_id: int,
    entry_id: int,
    status: RosterLinkStatus,
    roster_entry_id: int | None,
    by_player_id: int | None,
) -> RosterLink:
    link = await _link(session, tenant_id, player_id, entry_id)
    if link is None:
        link = RosterLink(tenant_id=tenant_id, player_id=player_id, team_entry_id=entry_id)
        session.add(link)
    link.status, link.season_roster_entry_id = status, roster_entry_id
    if status in DONE:
        link.confirmed_by_player_id, link.confirmed_at = by_player_id, utcnow()
    await session.flush()
    return link


async def confirm(
    session: AsyncSession,
    tenant_id: int,
    player_id: int,
    entry_id: int,
    roster_entry_id: int,
    by_player_id: int,
) -> RosterLink:
    entry = await session.get(TeamEntry, entry_id)
    owner = (await _taken(session, tenant_id, entry)).get(roster_entry_id)
    if owner is not None and owner != player_id:
        raise MatchConflict(f"строка заявки уже связана с игроком {owner}")
    return await _set_link(
        session, tenant_id, player_id, entry_id, RosterLinkStatus.CONFIRMED, roster_entry_id, by_player_id
    )


async def mark_not_in_roster(
    session: AsyncSession, tenant_id: int, player_id: int, entry_id: int, by_player_id: int
) -> RosterLink:
    return await _set_link(
        session, tenant_id, player_id, entry_id, RosterLinkStatus.NOT_IN_ROSTER, None, by_player_id
    )


async def mark_not_team_player(session: AsyncSession, tenant_id: int, player_id: int) -> None:
    player = await session.get(Player, player_id)
    if player is not None and player.tenant_id == tenant_id:
        player.active = False


async def unambiguous(session: AsyncSession, tenant_id: int) -> list[QueueItem]:
    """Однозначные: ровно один кандидат «точно» и на эту строку не претендует никто другой в очереди."""
    items = [
        i
        for i in await queue(session, tenant_id)
        if len(i.candidates) == 1 and i.candidates[0].confidence == Confidence.EXACT
    ]
    claims: dict[tuple[int, object], int] = {}
    for i in items:
        claims[(i.entry.id, i.candidates[0].key)] = claims.get((i.entry.id, i.candidates[0].key), 0) + 1
    return [i for i in items if claims[(i.entry.id, i.candidates[0].key)] == 1]


async def confirm_all_unambiguous(session: AsyncSession, tenant_id: int, by_player_id: int) -> int:
    count = 0
    for item in await unambiguous(session, tenant_id):
        await confirm(session, tenant_id, item.player.id, item.entry.id, item.candidates[0].key, by_player_id)
        count += 1
    return count


async def roster_status(session: AsyncSession, tenant_id: int, player_id: int, entry_id: int) -> str:
    """in_roster / not_in_roster / unmatched (domain.md)."""
    link = await _link(session, tenant_id, player_id, entry_id)
    if link is None or link.status not in DONE:
        return "unmatched"
    return "in_roster" if link.status == RosterLinkStatus.CONFIRMED else "not_in_roster"


async def roster_entry_for(
    session: AsyncSession, tenant_id: int, player_id: int, entry_id: int
) -> SeasonRosterEntry | None:
    link = await _link(session, tenant_id, player_id, entry_id)
    if link is None or link.status != RosterLinkStatus.CONFIRMED or link.season_roster_entry_id is None:
        return None
    return await session.get(SeasonRosterEntry, link.season_roster_entry_id)


async def import_chat_members(
    session: AsyncSession,
    tenant_id: int,
    members: list[dict],
    confirmed: list[dict],
    confirmed_by_vk_id: int | None,
) -> dict[str, int]:
    """Первичная загрузка: участники беседы → игроки; подтверждения руководителя → подтверждённые связи;
    однозначные совпадения → «предложено» (их подтверждают кнопкой «Подтвердить все однозначные»)."""
    stats = {"players": 0, "confirmed": 0, "suggested": 0}
    by_vk = {str(c["vk_id"]): c for c in confirmed}
    leader = None
    if confirmed_by_vk_id is not None:
        acc = (
            await session.execute(
                select(MessengerAccount).filter_by(
                    tenant_id=tenant_id, channel=Channel.VK, external_user_id=str(confirmed_by_vk_id)
                )
            )
        ).scalar_one_or_none()
        leader = acc.player_id if acc else None
    entries = (await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id))).all()
    for m in members:
        if m.get("deactivated"):
            continue
        vk_id = str(m["vk_id"])
        acc = (
            await session.execute(
                select(MessengerAccount).filter_by(
                    tenant_id=tenant_id, channel=Channel.VK, external_user_id=vk_id
                )
            )
        ).scalar_one_or_none()
        if acc is None:
            player = Player(tenant_id=tenant_id, display_name=f"{m['first_name']} {m['last_name']}".strip())
            session.add(player)
            await session.flush()
            session.add(
                MessengerAccount(
                    tenant_id=tenant_id, player_id=player.id, channel=Channel.VK, external_user_id=vk_id
                )
            )
            stats["players"] += 1
        else:
            player = await session.get(Player, acc.player_id)
            player.display_name = f"{m['first_name']} {m['last_name']}".strip()  # имя — из профиля VK
        await session.flush()
        for entry in entries:
            if (
                link := await _link(session, tenant_id, player.id, entry.id)
            ) is not None and link.status in DONE:
                continue
            if vk_id in by_vk:
                want = (norm(by_vk[vk_id]["last_name"]), norm(by_vk[vk_id]["first_name"]))
                row = next(
                    (
                        r
                        for r in await roster_rows(session, entry)
                        if (norm(r.last_name), norm(r.first_name)) == want
                    ),
                    None,
                )
                if row is not None:
                    await confirm(session, tenant_id, player.id, entry.id, row.id, leader)
                    stats["confirmed"] += 1
                continue
            cands = await candidates(session, tenant_id, player, entry)
            if len(cands) == 1 and cands[0].confidence == Confidence.EXACT:
                await _set_link(
                    session, tenant_id, player.id, entry.id, RosterLinkStatus.SUGGESTED, cands[0].key, None
                )
                stats["suggested"] += 1
    await session.commit()
    return stats
