"""Беседа команды в мессенджере: у команды одна активная беседа на канал (переезд беседы — смена в конфиге)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.storage.tables import Chat


async def active_chat(session: AsyncSession, tenant_id: int, channel: str) -> Chat | None:
    return (
        await session.scalars(
            select(Chat).filter_by(tenant_id=tenant_id, channel=channel, active=True).order_by(Chat.id.desc())
        )
    ).first()
