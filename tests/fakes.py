"""Фальшивый канал для тестов сервисов: запоминает, что бот отправил."""

from basket_assist.channels.base import Button, ButtonPress, ChannelError, MessageRef


class FakeChannel:
    channel = "vk"

    def __init__(self, names: dict[str, str] | None = None, editable: bool = True):
        self.names = names or {}
        self.editable = editable
        self.sent: list[tuple[str, str, list | None]] = []
        self.edits: list[tuple[MessageRef, str]] = []
        self.pins: list[MessageRef] = []
        self.toasts: list[str] = []
        self.links: list[str | None] = []  # ссылка в ответе на нажатие (open_link), параллельно toasts
        self.messages: dict[tuple[str, str], str] = {}
        self.buttons: dict[tuple[str, str], list | None] = {}  # клавиатура сообщения (последняя)
        self.images: list[
            tuple[str, bytes, str, list | None]
        ] = []  # send_image: (peer, png, подпись, кнопки)
        self.image_error: ChannelError | None = None  # если задано — send_image падает с этой ошибкой
        self._next = 100

    async def events(self):
        if False:
            yield

    async def send(self, peer_id: str, text: str, buttons: list[list[Button]] | None = None) -> MessageRef:
        self._next += 1
        ref = MessageRef(peer_id, str(self._next))
        self.sent.append((peer_id, text, buttons))
        self.messages[(peer_id, ref.message_id)] = text
        self.buttons[(peer_id, ref.message_id)] = buttons
        return ref

    async def send_image(
        self, peer_id: str, image: bytes, text: str = "", buttons: list[list[Button]] | None = None
    ) -> MessageRef:
        if self.image_error is not None:
            raise self.image_error
        self._next += 1
        ref = MessageRef(peer_id, str(self._next))
        self.images.append((peer_id, image, text, buttons))
        self.messages[(peer_id, ref.message_id)] = text
        self.buttons[(peer_id, ref.message_id)] = buttons
        return ref

    async def edit(self, msg: MessageRef, text: str, buttons=None) -> bool:
        if not self.editable:
            self.editable = True  # старое не редактируется, новое — да
            return False
        self.edits.append((msg, text))
        self.messages[(msg.peer_id, msg.message_id)] = text
        self.buttons[(msg.peer_id, msg.message_id)] = buttons
        return True

    async def pin(self, msg: MessageRef) -> bool:
        self.pins.append(msg)
        return True

    async def answer_button(self, press: ButtonPress, toast: str, link: str | None = None) -> None:
        self.toasts.append(toast)
        self.links.append(link)

    async def chat_members(self, peer_id: str) -> list[str]:
        return list(self.names)

    async def user_names(self, user_ids: list[str]) -> dict[str, str]:
        return {u: self.names.get(u, f"id{u}") for u in user_ids}

    def connect_link(self, ref: str) -> str:
        return f"https://vk.me/club1?ref={ref}"
