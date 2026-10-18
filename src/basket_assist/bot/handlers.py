"""Обработка входящих событий канала.

Приватность (ADR 0004): в беседе бот реагирует только на обращения к себе, свои кнопки и
служебные события. Остальные сообщения не обрабатываются, не сохраняются и не логируются.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.channels.base import ButtonPress, ChannelAdapter, DmAllowed, Event, IncomingMessage
from basket_assist.services import polls
from basket_assist.services.players import set_dm_allowed
from basket_assist.services.schedule import upcoming_text
from basket_assist.storage.tables import Chat, MessengerAccount, Tenant

log = logging.getLogger(__name__)

HELP_CHAT = (
    "Я помогаю команде: анонсы матчей и голосование «Буду / Не буду», напоминания, "
    "заявка на игру для капитана.\n"
    "Команды (обратитесь ко мне): «игры» — ближайшие матчи, «подключиться» — ссылка для личных сообщений."
)
WELCOME_DM = (
    "Привет! Вы подключены к Basket Assist.\n"
    "Сюда буду присылать напоминания об играх и важные сообщения по заявке.\n"
    "Я храню только то, что нужно для заявки на игру: имя, ID VK, ваши отметки «Буду / Не буду». "
    "Переписку беседы я не читаю и не сохраняю.\n"
    "Команды: «игры» — ближайшие матчи."
)


async def _tenant_for_chat(session: AsyncSession, channel: ChannelAdapter, peer_id: str) -> int | None:
    chat = (
        await session.execute(select(Chat).filter_by(channel=channel.channel, peer_id=peer_id))
    ).scalar_one_or_none()
    return chat.tenant_id if chat else None


async def _tenant_for_user(session: AsyncSession, channel: ChannelAdapter, user_id: str) -> int | None:
    account = (
        (
            await session.execute(
                select(MessengerAccount).filter_by(channel=channel.channel, external_user_id=user_id)
            )
        )
        .scalars()
        .first()
    )
    if account:
        return account.tenant_id
    tenants = (await session.scalars(select(Tenant.id))).all()
    return tenants[0] if len(tenants) == 1 else None  # MVP: одна команда сервиса


async def handle(session: AsyncSession, channel: ChannelAdapter, event: Event) -> None:
    match event:
        case ButtonPress():
            tenant_id = await _tenant_for_chat(session, channel, event.peer_id) or await _tenant_for_user(
                session, channel, event.user_id
            )
            if tenant_id is None:
                return
            if "m" in event.payload:
                await polls.on_vote(session, channel, tenant_id, event)
        case DmAllowed():
            tenant_id = await _tenant_for_user(session, channel, event.user_id)
            if tenant_id is not None:
                await set_dm_allowed(session, tenant_id, channel, event.user_id, event.allowed)
                await session.commit()
        case IncomingMessage(is_chat=True):
            if not event.mentioned:
                return  # чужие сообщения беседы игнорируем полностью
            tenant_id = await _tenant_for_chat(session, channel, event.peer_id)
            if tenant_id is None:
                log.info("обращение из неизвестной беседы %s — игнорирую", event.peer_id)
                return
            await _chat_command(session, channel, tenant_id, event)
        case IncomingMessage(is_chat=False):
            tenant_id = await _tenant_for_user(session, channel, event.user_id)
            if tenant_id is None:
                return
            await _dm(session, channel, tenant_id, event)


async def _chat_command(session, channel: ChannelAdapter, tenant_id: int, event: IncomingMessage) -> None:
    cmd = event.text.lower().strip(" .!?")
    if cmd in ("игры", "матчи", "расписание"):
        await channel.send(event.peer_id, await upcoming_text(session, tenant_id))
    elif cmd in ("подключиться", "старт", "начать"):
        await channel.send(
            event.peer_id,
            f"Чтобы получать напоминания в личку, откройте ссылку и нажмите «Начать»: "
            f"{channel.connect_link('chat')}",
        )
    else:
        await channel.send(event.peer_id, HELP_CHAT)


async def _dm(session, channel: ChannelAdapter, tenant_id: int, event: IncomingMessage) -> None:
    started = event.payload.get("command") == "start" or event.ref is not None
    await set_dm_allowed(session, tenant_id, channel, event.user_id, True)  # раз пишет — личка открыта
    await session.commit()
    cmd = event.text.lower().strip(" .!?")
    if started or cmd in ("начать", "старт", "start"):
        await channel.send(event.peer_id, WELCOME_DM)
    elif cmd in ("игры", "матчи", "расписание"):
        await channel.send(event.peer_id, await upcoming_text(session, tenant_id))
    else:
        await channel.send(event.peer_id, "Команды: «игры» — ближайшие матчи.")
