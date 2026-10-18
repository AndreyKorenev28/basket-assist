"""Интерфейс канала (мессенджера) и входящие события. Бизнес-логика знает только про это."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Button:
    label: str
    payload: dict
    color: str = "secondary"  # positive / negative / primary / secondary


@dataclass(frozen=True)
class MessageRef:
    peer_id: str
    message_id: str  # в VK — conversation_message_id


@dataclass(frozen=True)
class IncomingMessage:
    peer_id: str
    user_id: str
    text: str  # без упоминания бота
    is_chat: bool
    mentioned: bool  # в беседе: обращение к боту
    ref: str | None = None  # метка из ссылки «Подключиться»
    payload: dict = field(default_factory=dict)  # payload кнопки «Начать» и т. п.
    action: str | None = None  # служебное событие беседы (chat_invite_user и т. п.)
    action_member_id: str | None = None  # кого добавили/исключили


@dataclass(frozen=True)
class ButtonPress:
    peer_id: str
    user_id: str
    message: MessageRef
    payload: dict
    event_id: str


@dataclass(frozen=True)
class DmAllowed:
    user_id: str
    allowed: bool


Event = IncomingMessage | ButtonPress | DmAllowed


class ChannelError(Exception):
    def __init__(self, code: int | None, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code


class ChannelAdapter(Protocol):
    channel: str

    def events(self) -> AsyncIterator[Event]: ...
    async def send(
        self, peer_id: str, text: str, buttons: list[list[Button]] | None = None
    ) -> MessageRef: ...
    async def send_image(
        self, peer_id: str, image: bytes, text: str = "", buttons: list[list[Button]] | None = None
    ) -> MessageRef:
        """Сообщение с картинкой PNG. Может упасть с ChannelError — тогда вызывающий шлёт текст."""
        ...

    async def edit(self, msg: MessageRef, text: str, buttons: list[list[Button]] | None = None) -> bool: ...
    async def pin(self, msg: MessageRef) -> bool: ...
    async def answer_button(self, press: ButtonPress, toast: str, link: str | None = None) -> None:
        """Ответ на нажатие: всплывающая подсказка `toast` или переход по ссылке `link`."""
        ...

    async def chat_members(self, peer_id: str) -> list[str]: ...
    async def user_names(self, user_ids: list[str]) -> dict[str, str]: ...
    def connect_link(self, ref: str) -> str: ...  # ссылка на личку с ботом с меткой ref
