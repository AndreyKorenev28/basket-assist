"""Фальшивый канал для тестов сервисов: запоминает, что бот отправил."""

from basket_assist.channels.base import Button, ButtonPress, MessageRef


class FakeChannel:
    channel = "vk"

    def __init__(self, names: dict[str, str] | None = None, editable: bool = True):
        self.names = names or {}
        self.editable = editable
        self.sent: list[tuple[str, str, list | None]] = []
        self.edits: list[tuple[MessageRef, str]] = []
        self.pins: list[MessageRef] = []
        self.toasts: list[str] = []
        self.messages: dict[tuple[str, str], str] = {}
        self._next = 100

    async def events(self):
        if False:
            yield

    async def send(self, peer_id: str, text: str, buttons: list[list[Button]] | None = None) -> MessageRef:
        self._next += 1
        ref = MessageRef(peer_id, str(self._next))
        self.sent.append((peer_id, text, buttons))
        self.messages[(peer_id, ref.message_id)] = text
        return ref

    async def edit(self, msg: MessageRef, text: str, buttons=None) -> bool:
        if not self.editable:
            self.editable = True  # старое не редактируется, новое — да
            return False
        self.edits.append((msg, text))
        self.messages[(msg.peer_id, msg.message_id)] = text
        return True

    async def pin(self, msg: MessageRef) -> bool:
        self.pins.append(msg)
        return True

    async def answer_button(self, press: ButtonPress, toast: str) -> None:
        self.toasts.append(toast)

    async def chat_members(self, peer_id: str) -> list[str]:
        return list(self.names)

    async def user_names(self, user_ids: list[str]) -> dict[str, str]:
        return {u: self.names.get(u, f"id{u}") for u in user_ids}

    def connect_link(self, ref: str) -> str:
        return f"https://vk.me/club1?ref={ref}"
