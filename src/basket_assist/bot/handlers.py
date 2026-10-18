"""Обработка входящих событий канала.

Приватность (ADR 0004): в беседе бот реагирует только на обращения к себе, свои кнопки и
служебные события. Остальные сообщения не обрабатываются, не сохраняются и не логируются.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.bot import leader
from basket_assist.channels.base import (
    Button,
    ButtonPress,
    ChannelAdapter,
    ChannelError,
    DmAllowed,
    Event,
    IncomingMessage,
)
from basket_assist.services import leaders, notify, polls, roster
from basket_assist.services.players import get_or_create_player, set_dm_allowed
from basket_assist.services.schedule import upcoming_text
from basket_assist.storage.tables import Chat, MessengerAccount, TeamEntry, Tenant

log = logging.getLogger(__name__)

HELP_WORDS = ("помощь", "команды", "help", "")  # "" — сообщение из одних «?»/«!»
UNKNOWN = "Не понял. Напишите «помощь» — покажу, что умею."
HELP_CHAT = (
    "Я помогаю команде: анонсы матчей с голосованием «Буду / Не буду», напоминания, "
    "заявка на игру для капитана.\n\n"
    "Обратитесь ко мне:\n"
    "• игры — ближайшие матчи\n"
    "• бот — перейти в личку с ботом: напоминания и ваши команды\n"
    "• помощь — этот список\n\n"
    "Остальные команды — в личке со мной."
)
HELP_DM = (
    "Что я умею:\n\n"
    "• игры — ближайшие матчи: дата, время, зал, соперник\n"
    "• мои данные — к каким заявкам на сезон вы привязаны и под каким номером "
    "(если что-то не так — там есть кнопка «Это ошибка»)\n"
    "• помощь — этот список\n\n"
    "Отметиться на игру — кнопками «Буду / Не буду» в анонсе в беседе команды."
)
HELP_LEADER = (
    "Для руководителей команды:\n\n"
    "• состав — кто отметился на ближайшую игру в каждой лиге\n"
    "• игроки — все игроки и их статусы по лигам\n"
    "• сопоставление — привязать новых игроков к заявкам на сезон\n"
    "• исправить Петров — изменить привязку игрока (по имени или фамилии)"
)
HELP_RIGHTS = "\n• права — назначить или снять и. о. капитана"  # только капитану и заместителю
HELP_BUTTONS_HINT = "\n\nКоманду можно написать или нажать кнопку ниже."


def _cmd(label: str, command: str, color: str = "primary") -> Button:
    """Кнопка-команда: нажатие выполняет то же, что текст `command` (с теми же проверками прав)."""
    return Button(label, {"c": command}, color)


GO_WORDS = ("бот", "перейти к боту", "подключиться", "старт", "начать", "личка")
CHAT_BUTTONS = [[_cmd("Игры", "игры"), _cmd("Перейти к боту", "бот")]]
UNKNOWN_CHAT = (
    "Эта беседа пока не подключена к Basket Assist, поэтому здесь я не отвечаю на команды "
    "и не публикую анонсы. Подключение настраивает администратор сервиса."
)
UNKNOWN_BUTTONS = [[_cmd("Помощь", "помощь", "secondary")]]
WELCOME_DM = (
    "Привет! Вы подключены к Basket Assist.\n"
    "Сюда буду присылать напоминания об играх и важные сообщения по заявке.\n"
    "Я храню только то, что нужно для заявки на игру: имя, ID VK, ваши отметки «Буду / Не буду» "
    "и связь с заявкой на сезон. Переписку беседы я не читаю и не сохраняю.\n"
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
            chat_tenant = await _tenant_for_chat(session, channel, event.peer_id)
            if chat_tenant is None and event.peer_id != event.user_id:
                return  # кнопка в неподключённой беседе (в личке peer_id = user_id) — не обрабатываем
            tenant_id = chat_tenant or await _tenant_for_user(session, channel, event.user_id)
            if tenant_id is None:
                return
            if "m" in event.payload:
                await polls.on_vote(session, channel, tenant_id, event)
            elif "c" in event.payload:
                await _command_button(session, channel, tenant_id, event, in_chat=chat_tenant is not None)
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
                if event.mentioned:  # отвечаем, чтобы молчание не выглядело поломкой; данные не сохраняем
                    log.info("обращение из неподключённой беседы %s", event.peer_id)
                    await channel.send(event.peer_id, UNKNOWN_CHAT)
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
    await _chat_reply(session, channel, tenant_id, event.peer_id, event.text)


async def _chat_reply(session, channel: ChannelAdapter, tenant_id: int, peer_id: str, text: str) -> None:
    """Команда в беседе — из обращения к боту или с кнопки."""
    cmd = text.lower().strip(" .!?")
    if cmd in ("игры", "матчи", "расписание"):
        await channel.send(peer_id, await upcoming_text(session, tenant_id))
    elif cmd in GO_WORDS:
        await channel.send(
            peer_id,
            f"Личка с ботом — напоминания об играх и ваши команды. Откройте ссылку "
            f"(в первый раз нажмите «Начать»): {channel.connect_link('chat')}",
        )
    elif cmd in HELP_WORDS:
        await channel.send(peer_id, HELP_CHAT, CHAT_BUTTONS)
    else:
        await channel.send(peer_id, UNKNOWN, UNKNOWN_BUTTONS)


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
    await _dm_command(session, channel, tenant_id, event.peer_id, event.user_id, event.text, started)


async def _dm_command(
    session,
    channel: ChannelAdapter,
    tenant_id: int,
    peer_id: str,
    user_id: str,
    text: str,
    started: bool = False,
) -> None:
    """Команда в личке — из текста или с кнопки. Права проверяются здесь, одинаково для обоих путей."""
    player = await set_dm_allowed(session, tenant_id, channel, user_id, True)  # раз пишет — личка открыта
    await session.commit()
    is_leader = await leaders.is_leader(session, tenant_id, player.id)
    cmd = text.lower().strip(" .!?")
    if started or cmd in ("начать", "старт", "start"):
        help_text, buttons = await _help_dm(session, player.id, is_leader)
        await channel.send(peer_id, WELCOME_DM + "\n" + help_text, buttons)
    elif cmd in HELP_WORDS:
        await channel.send(peer_id, *await _help_dm(session, player.id, is_leader))
    elif cmd in ("игры", "матчи", "расписание"):
        await channel.send(peer_id, await upcoming_text(session, tenant_id))
    elif cmd in ("мои данные", "данные"):
        text, buttons = await _my_data(session, channel, tenant_id, player.id)
        await channel.send(peer_id, text, buttons)
    elif cmd in ("сопоставление", "сопоставить") and is_leader:
        await channel.send(peer_id, *await leader.matching_summary(session, tenant_id))
    elif cmd in ("состав", "кто идёт", "кто идет") and is_leader:
        await channel.send(peer_id, *await leader.lineup(session, tenant_id))
    elif cmd in ("игроки", "список") and is_leader:
        await _players(session, channel, tenant_id, peer_id)
    elif cmd in PLAYERS_TEXT_WORDS and is_leader:
        await _players(session, channel, tenant_id, peer_id, as_text=True)
    elif cmd.startswith("исправить") and is_leader:
        query = text.strip()[len("исправить") :].strip()
        if not query:  # без имени — общий список, из него видно, кого исправлять
            await _players(session, channel, tenant_id, peer_id)
        else:
            await channel.send(peer_id, *await leader.find_players(session, tenant_id, query))
    elif cmd in ("права", "и. о.", "ио") and is_leader:
        if await leaders.is_captain_or_vice(session, player.id):
            await channel.send(peer_id, *await leader.rights_menu(session, tenant_id))
        else:
            await channel.send(peer_id, "Передавать права может только капитан или заместитель.")
    else:
        await channel.send(peer_id, UNKNOWN, UNKNOWN_BUTTONS)


PLAYERS_TEXT_WORDS = ("игроки текстом", "список текстом", "текстом")


async def _players(
    session, channel: ChannelAdapter, tenant_id: int, peer_id: str, as_text: bool = False
) -> None:
    """«игроки»: картинкой (ровные колонки на любом телефоне), при сбое или по просьбе — текстом."""
    if not as_text:
        image, caption = await leader.players_image(session, tenant_id)
        if image is not None:
            try:
                await channel.send_image(
                    peer_id, image, caption, [[_cmd("Списком текстом", "игроки текстом", "secondary")]]
                )
                return
            except ChannelError as e:
                log.warning("не удалось отправить «игроки» картинкой: %s — шлю текстом", e)
    for part in await leader.players_list(session, tenant_id):
        await channel.send(peer_id, part)


async def _help_dm(session, player_id: int, is_leader: bool) -> tuple[str, list[list[Button]]]:
    """Список команд в личке и кнопки к нему: игроку — общие, руководителям — ещё свои («права» — без и. о.)."""
    buttons = [[_cmd("Игры", "игры"), _cmd("Мои данные", "мои данные")]]
    if not is_leader:
        return HELP_DM + HELP_BUTTONS_HINT, buttons
    buttons.append([_cmd("Состав", "состав", "secondary"), _cmd("Игроки", "игроки", "secondary")])
    last_row = [_cmd("Сопоставление", "сопоставление", "secondary")]
    rights = ""
    if await leaders.is_captain_or_vice(session, player_id):
        rights = HELP_RIGHTS
        last_row.append(_cmd("Права", "права", "secondary"))
    buttons.append(last_row)
    return f"{HELP_DM}\n\n{HELP_LEADER}{rights}{HELP_BUTTONS_HINT}", buttons


async def _command_button(
    session, channel: ChannelAdapter, tenant_id: int, press: ButtonPress, *, in_chat: bool
) -> None:
    """Кнопка-команда из подсказки: выполняем как текстовую команду, сообщение с подсказкой не трогаем."""
    command = str(press.payload.get("c", ""))
    if in_chat and command in GO_WORDS:
        await _go_to_bot(session, channel, tenant_id, press)
        return
    await channel.answer_button(press, "")
    if in_chat:
        await _chat_reply(session, channel, tenant_id, press.peer_id, command)
    else:
        await _dm_command(session, channel, tenant_id, press.peer_id, press.user_id, command)


async def _go_to_bot(session, channel: ChannelAdapter, tenant_id: int, press: ButtonPress) -> None:
    """«Перейти к боту» в беседе: открываем личку, а подключённому сразу присылаем туда меню по роли.

    Не подключённый увидит в личке кнопку «Начать» — меню придёт в ответ на неё (приветствие).
    """
    account = (
        await session.execute(
            select(MessengerAccount).filter_by(
                tenant_id=tenant_id, channel=channel.channel, external_user_id=press.user_id
            )
        )
    ).scalar_one_or_none()
    if account is not None and account.dm_allowed:
        is_leader = await leaders.is_leader(session, tenant_id, account.player_id)
        try:
            await channel.send(press.user_id, *await _help_dm(session, account.player_id, is_leader))
        except ChannelError as e:  # запретил сообщения, а событие message_deny мы пропустили
            log.warning("не удалось написать в личку %s: %s", press.user_id, e)
    await channel.answer_button(press, "", link=channel.connect_link("chat"))


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
