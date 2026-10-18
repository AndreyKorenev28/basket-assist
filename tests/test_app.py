"""app.run_bot.process: лог нажатий без ID пользователя и ответ на нажатие при ошибке обработчика.

process — замыкание внутри run_bot, поэтому гоняем run_bot целиком: канал, HTTP-клиент и планировщик
подменены, канал отдаёт события и ждёт, пока они обработаются. Вымышленные ID."""

import asyncio
import contextlib
import logging
from types import SimpleNamespace

import pytest

from basket_assist import app
from basket_assist.channels.base import ButtonPress, IncomingMessage, MessageRef
from tests.fakes import FakeChannel

USER = "424242"
CHAT_PEER = "2000000001"


class EventsChannel(FakeChannel):
    def __init__(self, events, fail_answer: bool = False):
        super().__init__()
        self._events = events
        self.fail_answer = fail_answer
        self.handled = 0

    async def events(self):
        for e in self._events:
            yield e
        for _ in range(200):  # ждём, пока задачи process доработают
            if self.handled >= len(self._events):
                break
            await asyncio.sleep(0.01)

    async def answer_button(self, press, toast, link=None):
        if self.fail_answer:
            raise RuntimeError("VK недоступен")
        await super().answer_button(press, toast, link)


@pytest.fixture
def run(monkeypatch, db_url):
    async def _run(events, handler, fail_answer: bool = False) -> EventsChannel:
        ch = EventsChannel(events, fail_answer)

        async def handle(session, channel, event):
            try:
                await handler(session, channel, event)
            finally:
                ch.handled += 1

        async def idle(*args, **kwargs):
            await asyncio.Event().wait()

        @contextlib.asynccontextmanager
        async def client():
            yield None

        monkeypatch.setattr(app, "handle", handle)
        monkeypatch.setattr(app, "make_vk_channel", lambda settings, c: ch)
        monkeypatch.setattr(app, "make_client", client)
        monkeypatch.setattr(app.scheduler, "run", idle)
        await app.run_bot(SimpleNamespace(db_url=db_url))
        await asyncio.sleep(0.05)  # дать отработать except-веткам после handle
        return ch

    return _run


def _press(peer: str, payload: dict) -> ButtonPress:
    return ButtonPress(peer, USER, MessageRef(peer, "7"), payload, "ev1")


async def _ok(session, channel, event):
    return None


async def _boom(session, channel, event):
    raise ValueError("сбой")


async def test_press_logged_without_user_id(run, caplog):
    caplog.set_level(logging.INFO, logger="basket_assist.app")
    await run(
        [
            _press(USER, {"a": "dg", "m": 5, "c": "состав", "p": 77, "secret": "x"}),
            _press(CHAT_PEER, {"m": 5, "v": "going"}),
            IncomingMessage(USER, USER, "привет", is_chat=False, mentioned=False),
        ],
        _ok,
    )
    presses = [r.getMessage() for r in caplog.records if r.getMessage().startswith("нажатие")]
    assert presses == [
        "нажатие (личка): {'a': 'dg', 'c': 'состав'}",
        f"нажатие (беседа {CHAT_PEER}): {{'v': 'going'}}",
    ]
    assert all(USER not in r.getMessage() for r in caplog.records)


async def test_press_error_answers_button(run, caplog):
    caplog.set_level(logging.INFO, logger="basket_assist.app")
    ch = await run(
        [_press(CHAT_PEER, {"m": 5, "v": "going"}), IncomingMessage(USER, USER, "x", False, False)], _boom
    )
    assert ch.toasts == ["Не получилось, попробуйте ещё раз"], "на сообщение (не кнопку) не отвечаем"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert {r.getMessage() for r in errors} == {
        "ошибка обработки события ButtonPress",
        "ошибка обработки события IncomingMessage",
    }


async def test_press_error_and_answer_fails_only_warning(run, caplog):
    caplog.set_level(logging.INFO, logger="basket_assist.app")
    ch = await run([_press(USER, {"a": "dg"})], _boom, fail_answer=True)
    assert ch.toasts == []
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "не удалось ответить на нажатие после ошибки"
    ]
