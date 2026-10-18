"""Отправка «ровно один раз» через notification_log и личные сообщения игрокам."""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.channels.base import Button, ChannelAdapter, ChannelError
from basket_assist.storage.tables import MessengerAccount, NotificationLog

log = logging.getLogger(__name__)


async def once(
    session: AsyncSession,
    tenant_id: int,
    kind: str,
    dedup_key: str,
    *,
    match_id: int | None = None,
    player_id: int | None = None,
) -> bool:
    """Записать факт отправки. False — уже отправляли (уникальный ключ)."""
    try:
        async with session.begin_nested():
            session.add(
                NotificationLog(
                    tenant_id=tenant_id,
                    kind=kind,
                    dedup_key=dedup_key,
                    match_id=match_id,
                    player_id=player_id,
                )
            )
    except IntegrityError:
        return False
    return True


async def was_sent(session: AsyncSession, tenant_id: int, kind: str, dedup_key: str) -> bool:
    """Отправляли ли уже (есть ли запись в журнале) — без записи."""
    found = await session.scalar(
        select(NotificationLog.id).filter_by(tenant_id=tenant_id, kind=kind, dedup_key=dedup_key)
    )
    return found is not None


async def dm(
    session: AsyncSession,
    channel: ChannelAdapter,
    tenant_id: int,
    player_id: int,
    text: str,
    buttons: list[list[Button]] | None = None,
) -> bool:
    """Личное сообщение игроку, если он разрешил (нажал «Начать»). False — не доставлено."""
    account = (
        await session.execute(
            select(MessengerAccount).filter_by(
                tenant_id=tenant_id, player_id=player_id, channel=channel.channel
            )
        )
    ).scalar_one_or_none()
    if account is None or not account.dm_allowed:
        return False
    try:
        await channel.send(account.external_user_id, text, buttons)
    except ChannelError as e:
        log.warning("личка игроку %s не доставлена: %s", player_id, e)
        return False
    return True
