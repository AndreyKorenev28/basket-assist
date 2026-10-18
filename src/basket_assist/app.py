"""Запуск бота: Long Poll → обработчики, у каждого события своя сессия БД."""

import asyncio
import logging

from sqlalchemy import select

from basket_assist.bot.handlers import handle
from basket_assist.channels.base import ChannelAdapter
from basket_assist.channels.vk import VkChannel, VkClient
from basket_assist.config import Settings
from basket_assist.services import polls
from basket_assist.sources.base import make_client
from basket_assist.storage import db
from basket_assist.storage.tables import Chat, Match, TeamEntry

log = logging.getLogger(__name__)


def make_vk_channel(settings: Settings, client) -> VkChannel:
    if not settings.vk_group_token or not settings.vk_group_id:
        raise SystemExit("В .env нужны VK_GROUP_TOKEN и VK_GROUP_ID")
    return VkChannel(VkClient(settings.vk_group_token.get_secret_value(), client), settings.vk_group_id)


async def run_bot(settings: Settings) -> None:
    engine = db.make_engine(settings.db_url)
    sessions = db.make_sessionmaker(engine)
    tasks: set[asyncio.Task] = set()

    async def process(channel: ChannelAdapter, event) -> None:
        try:
            async with sessions() as session:
                await handle(session, channel, event)
        except Exception:
            log.exception("ошибка обработки события %s", type(event).__name__)

    async with make_client() as client:
        channel = make_vk_channel(settings, client)
        log.info("бот запущен")
        try:
            async for event in channel.events():
                task = asyncio.create_task(process(channel, event))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            await engine.dispose()


async def post_poll(settings: Settings, match_id: int) -> str:
    """Опубликовать анонс-голосование вручную (до планировщика M4)."""
    engine = db.make_engine(settings.db_url)
    try:
        async with db.make_sessionmaker(engine)() as session, make_client() as client:
            channel = make_vk_channel(settings, client)
            match = await session.get(Match, match_id)
            if match is None:
                return f"Матч {match_id} не найден (номера матчей — в --show)"
            entry = (
                (await session.execute(select(TeamEntry).filter_by(tournament_id=match.tournament_id)))
                .scalars()
                .first()
            )
            chat = (
                (
                    await session.execute(
                        select(Chat).filter_by(tenant_id=entry.tenant_id, channel=channel.channel)
                    )
                )
                .scalars()
                .first()
            )
            if chat is None:
                return "Беседа команды не задана (vk_chat_peer_id в config/tenant.toml)"
            poll = await polls.publish(session, channel, chat, match)
            return f"Анонс опубликован в беседе {chat.peer_id}, сообщение {poll.message_ref}"
    finally:
        await engine.dispose()
