"""Отправка «ровно один раз» через notification_log и личные сообщения игрокам."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.channels.base import Button, ChannelAdapter, ChannelError
from basket_assist.storage.tables import MessengerAccount, NotificationLog

log = logging.getLogger(__name__)

# Администратор сервиса (не капитан): технические сигналы — пропали кнопки анонса, отметки текстом.
# ID пользователя в канале; задаётся при запуске бота из настроек (ADMIN_VK_USER_ID в .env).
_admin_user_id: str | None = None


def set_admin(user_id: str | None) -> None:
    global _admin_user_id
    _admin_user_id = user_id


async def admin(channel: ChannelAdapter, text: str) -> bool:
    """Сообщение администратору сервиса. Не задан или личка закрыта — только в лог."""
    if not _admin_user_id:
        log.warning("администратор сервиса не задан, сигнал только в лог: %s", text)
        return False
    try:
        await channel.send(_admin_user_id, text)
    except ChannelError as e:
        log.warning("сигнал администратору не доставлен: %s", e)
        return False
    return True


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
    return await sent_at(session, tenant_id, kind, dedup_key) is not None


async def sent_at(session: AsyncSession, tenant_id: int, kind: str, dedup_key: str) -> datetime | None:
    """Когда отправили (UTC без tzinfo) или None."""
    return await session.scalar(
        select(NotificationLog.sent_at).filter_by(tenant_id=tenant_id, kind=kind, dedup_key=dedup_key)
    )


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
