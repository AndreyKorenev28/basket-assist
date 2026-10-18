"""Обработка входящих событий канала.

Приватность (ADR 0004): в беседе бот реагирует только на обращения к себе, свои кнопки и
служебные события. Остальные сообщения не обрабатываются, не сохраняются и не логируются.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.bot import leader
from basket_assist.channels.base import Button, ButtonPress, ChannelAdapter, DmAllowed, Event, IncomingMessage
from basket_assist.services import leaders, notify, polls, roster
from basket_assist.services.players import get_or_create_player, set_dm_allowed
from basket_assist.services.schedule import upcoming_text
from basket_assist.storage.tables import Chat, MessengerAccount, TeamEntry, Tenant

log = logging.getLogger(__name__)

HELP_CHAT = (
    "Я помогаю команде: анонсы матчей и голосование «Буду / Не буду», напоминания, "
    "заявка на игру для капитана.\n"
    "Команды (обратитесь ко мне): «игры» — ближайшие матчи, «подключиться» — ссылка для личных сообщений."
)
WELCOME_DM = (
    "Привет! Вы подключены к Basket Assist.\n"
    "Сюда буду присылать напоминания об играх и важные сообщения по заявке.\n"
    "Я храню только то, что нужно для заявки на игру: имя, ID VK, ваши отметки «Буду / Не буду» "
    "и связь с заявкой на сезон. Переписку беседы я не читаю и не сохраняю.\n"
)
DM_COMMANDS = "Команды: «игры» — ближайшие матчи, «мои данные» — ваша привязка к заявкам на сезон."
LEADER_COMMANDS = (
    "Для руководителей: «игроки» — все игроки и их статусы по лигам, «сопоставление» — привязка игроков "
    "к заявкам на сезон, «исправить <имя>» — изменить привязку, «права» — и. о. капитана."
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
            elif "a" in event.payload:
                await _dm_button(session, channel, tenant_id, event)
        case DmAllowed():
            tenant_id = await _tenant_for_user(session, channel, event.user_id)
            if tenant_id is not None:
                await set_dm_allowed(session, tenant_id, channel, event.user_id, event.allowed)
                await session.commit()
        case IncomingMessage(is_chat=True):
            tenant_id = await _tenant_for_chat(session, channel, event.peer_id)
            if tenant_id is None:
                if event.mentioned:
                    log.info("обращение из неизвестной беседы %s — игнорирую", event.peer_id)
                return
            if event.action in ("chat_invite_user", "chat_invite_user_by_link"):
                await _new_member(session, channel, tenant_id, event)
                return
            if not event.mentioned:
                return  # чужие сообщения беседы игнорируем полностью
            await _chat_command(session, channel, tenant_id, event)
        case IncomingMessage(is_chat=False):
            tenant_id = await _tenant_for_user(session, channel, event.user_id)
            if tenant_id is None:
                return
            await _dm(session, channel, tenant_id, event)


async def _new_member(session, channel: ChannelAdapter, tenant_id: int, event: IncomingMessage) -> None:
    """Новый участник беседы → игрок в очереди сопоставления, руководителям — уведомление."""
    member = event.action_member_id
    if not member or member.startswith("-"):
        return
    player, _ = await get_or_create_player(session, tenant_id, channel, member)
    await session.commit()
    if await notify.once(session, tenant_id, "new_member", member, player_id=player.id):
        await session.commit()
        for lead in await leaders.leaders(session, tenant_id):
            await notify.dm(
                session,
                channel,
                tenant_id,
                lead.id,
                f"В беседу добавлен {player.display_name}. Сопоставьте с заявкой на сезон: напишите «сопоставление».",
            )


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


async def _my_data(session, channel: ChannelAdapter, tenant_id: int, player_id: int) -> tuple[str, list]:
    lines = ["Ваши данные в Basket Assist:"]
    for entry in (await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id))).all():
        label = await roster.entry_label(session, entry)
        status = await roster.roster_status(session, tenant_id, player_id, entry.id)
        if status == "in_roster":
            r = await roster.roster_entry_for(session, tenant_id, player_id, entry.id)
            num = f", №{r.number}" if r.number else ""
            lines.append(f"• {label}: {r.last_name} {r.first_name}{num}")
        elif status == "not_in_roster":
            lines.append(f"• {label}: нет в заявке на сезон")
        elif status == "not_in_league":
            lines.append(f"• {label}: не играет в этой лиге")
        else:
            lines.append(f"• {label}: ещё не сопоставлены")
    return "\n".join(lines), [[Button("Это ошибка", {"a": "err"}, "negative")]]


async def _dm(session, channel: ChannelAdapter, tenant_id: int, event: IncomingMessage) -> None:
    started = event.payload.get("command") == "start" or event.ref is not None
    player = await set_dm_allowed(
        session, tenant_id, channel, event.user_id, True
    )  # раз пишет — личка открыта
    await session.commit()
    is_leader = await leaders.is_leader(session, tenant_id, player.id)
    hint = DM_COMMANDS + ("\n" + LEADER_COMMANDS if is_leader else "")
    cmd = event.text.lower().strip(" .!?")
    if started or cmd in ("начать", "старт", "start"):
        await channel.send(event.peer_id, WELCOME_DM + hint)
    elif cmd in ("игры", "матчи", "расписание"):
        await channel.send(event.peer_id, await upcoming_text(session, tenant_id))
    elif cmd in ("мои данные", "данные"):
        text, buttons = await _my_data(session, channel, tenant_id, player.id)
        await channel.send(event.peer_id, text, buttons)
    elif cmd in ("сопоставление", "сопоставить") and is_leader:
        await channel.send(event.peer_id, *await leader.matching_summary(session, tenant_id))
    elif cmd in ("игроки", "список") and is_leader:
        for text in await leader.players_list(session, tenant_id):
            await channel.send(event.peer_id, text)
    elif cmd.startswith("исправить") and is_leader:
        query = event.text.strip()[len("исправить") :].strip()
        if not query:  # без имени — общий список, из него видно, кого исправлять
            for text in await leader.players_list(session, tenant_id):
                await channel.send(event.peer_id, text)
        else:
            await channel.send(event.peer_id, *await leader.find_players(session, tenant_id, query))
    elif cmd in ("права", "и. о.", "ио") and is_leader:
        if await leaders.is_captain_or_vice(session, player.id):
            await channel.send(event.peer_id, *await leader.rights_menu(session, tenant_id))
        else:
            await channel.send(event.peer_id, "Передавать права может только капитан или заместитель.")
    else:
        await channel.send(event.peer_id, hint)


async def _dm_button(session, channel: ChannelAdapter, tenant_id: int, press: ButtonPress) -> None:
    me, _ = await get_or_create_player(session, tenant_id, channel, press.user_id)
    if press.payload.get("a") == "err":  # «Это ошибка» в «Мои данные» — может нажать любой игрок
        await channel.answer_button(press, "Сообщил руководителям команды")
        for lead in await leaders.leaders(session, tenant_id):
            await notify.dm(
                session,
                channel,
                tenant_id,
                lead.id,
                f"{me.display_name} сообщает об ошибке в привязке к заявке на сезон. Напишите «сопоставление».",
            )
        return
    result = await leader.on_button(session, channel, tenant_id, me, press)
    if result is None:
        return
    text, buttons = result
    if not await channel.edit(press.message, text, buttons or None):
        await channel.send(press.peer_id, text, buttons or None)
