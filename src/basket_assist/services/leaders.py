"""Права капитана и их передача (domain.md, «Права капитана и их передача»).

Руководители команды: капитан, заместители и действующие и. о. капитана.
Срок действия проверяется при каждом запросе, поэтому истёкшее право не работает
даже до того, как планировщик пришлёт уведомление об истечении (M4).
"""

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.domain.models import Permission, Role, RosterLinkStatus
from basket_assist.storage.tables import PermissionGrant, Player, RosterLink, utcnow


class PermissionDenied(Exception):
    pass


def _active_grants(tenant_id: int, now: datetime):
    return select(PermissionGrant).where(
        PermissionGrant.tenant_id == tenant_id,
        PermissionGrant.permission == Permission.ACTING_CAPTAIN,
        PermissionGrant.revoked_at.is_(None),
        or_(PermissionGrant.valid_until.is_(None), PermissionGrant.valid_until > now),
    )


async def active_grants(session: AsyncSession, tenant_id: int) -> list[PermissionGrant]:
    return list((await session.scalars(_active_grants(tenant_id, utcnow()))).all())


async def leaders(session: AsyncSession, tenant_id: int) -> list[Player]:
    """Все, у кого есть права капитана сейчас."""
    base = (
        await session.scalars(
            select(Player).where(
                Player.tenant_id == tenant_id,
                Player.active.is_(True),
                Player.role.in_([Role.CAPTAIN, Role.VICE_CAPTAIN]),
            )
        )
    ).all()
    acting_ids = {g.player_id for g in await active_grants(session, tenant_id)}
    acting = [await session.get(Player, pid) for pid in acting_ids]
    out = {p.id: p for p in [*base, *acting] if p is not None and p.active}
    return list(out.values())


async def digest_recipients(session: AsyncSession, tenant_id: int) -> list[Player]:
    """Получатели сводок (M4): руководители минус те, кто попросил не присылать на время передачи прав."""
    muted = {
        g.granted_by_player_id
        for g in await active_grants(session, tenant_id)
        if g.suppress_digests_for_grantor
    }
    return [p for p in await leaders(session, tenant_id) if p.id not in muted]


async def is_leader(session: AsyncSession, tenant_id: int, player_id: int) -> bool:
    return any(p.id == player_id for p in await leaders(session, tenant_id))


async def is_captain_or_vice(session: AsyncSession, player_id: int) -> bool:
    p = await session.get(Player, player_id)
    return p is not None and p.role in (Role.CAPTAIN, Role.VICE_CAPTAIN)


async def is_matched(session: AsyncSession, tenant_id: int, player_id: int) -> bool:
    return (
        await session.execute(
            select(RosterLink.id).filter_by(
                tenant_id=tenant_id, player_id=player_id, status=RosterLinkStatus.CONFIRMED
            )
        )
    ).first() is not None


async def grant_acting(
    session: AsyncSession,
    tenant_id: int,
    grantor_id: int,
    grantee_id: int,
    valid_until: datetime | None,
    suppress_digests: bool = False,
) -> PermissionGrant:
    if not await is_captain_or_vice(session, grantor_id):
        raise PermissionDenied("передавать права может только капитан или заместитель")
    if not await is_matched(session, tenant_id, grantee_id):
        raise PermissionDenied("права можно передать только сопоставленному игроку")
    grant = PermissionGrant(
        tenant_id=tenant_id,
        player_id=grantee_id,
        permission=Permission.ACTING_CAPTAIN,
        granted_by_player_id=grantor_id,
        valid_until=valid_until,
        suppress_digests_for_grantor=suppress_digests,
    )
    session.add(grant)
    await session.flush()
    return grant


async def revoke(session: AsyncSession, tenant_id: int, grant_id: int, by_player_id: int) -> PermissionGrant:
    if not await is_captain_or_vice(session, by_player_id):
        raise PermissionDenied("отзывать права может только капитан или заместитель")
    grant = await session.get(PermissionGrant, grant_id)
    if grant is None or grant.tenant_id != tenant_id:
        raise PermissionDenied("передача прав не найдена")
    grant.revoked_at = utcnow()
    return grant
