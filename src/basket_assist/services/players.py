"""Игроки и их аккаунты в мессенджере."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.channels.base import ChannelAdapter
from basket_assist.domain.models import Role
from basket_assist.storage.tables import MessengerAccount, Player, utcnow


async def get_or_create_player(
    session: AsyncSession, tenant_id: int, channel: ChannelAdapter, user_id: str
) -> tuple[Player, MessengerAccount]:
    """Найти игрока по аккаунту мессенджера; если его нет — создать с именем из профиля."""
    account = (
        await session.execute(
            select(MessengerAccount).filter_by(
                tenant_id=tenant_id, channel=channel.channel, external_user_id=user_id
            )
        )
    ).scalar_one_or_none()
    if account is not None:
        return await session.get(Player, account.player_id), account

    names = await channel.user_names([user_id])
    player = Player(tenant_id=tenant_id, display_name=names.get(user_id, f"id{user_id}"), role=Role.PLAYER)
    session.add(player)
    await session.flush()
    account = MessengerAccount(
        tenant_id=tenant_id, player_id=player.id, channel=channel.channel, external_user_id=user_id
    )
    session.add(account)
    await session.flush()
    return player, account


async def set_dm_allowed(
    session: AsyncSession, tenant_id: int, channel: ChannelAdapter, user_id: str, allowed: bool
) -> Player:
    player, account = await get_or_create_player(session, tenant_id, channel, user_id)
    account.dm_allowed = allowed
    if allowed and account.connected_at is None:
        account.connected_at = utcnow()
    return player
