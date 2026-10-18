"""Канал VK: разбор событий Long Poll и вызовы API (на форматах из spikes/vk-probe)."""

import httpx
import pytest
import respx

from basket_assist.channels.base import ButtonPress, ChannelError, DmAllowed, IncomingMessage, MessageRef
from basket_assist.channels.vk import VkChannel, VkClient

GROUP = 242389357


def channel(client=None) -> VkChannel:
    return VkChannel(VkClient("t", client or httpx.AsyncClient()), GROUP)


def test_parse_chat_mention():
    ev = channel().parse_update(
        {
            "type": "message_new",
            "object": {
                "message": {"peer_id": 2000000001, "from_id": 10, "text": f"[club{GROUP}|@club{GROUP}] Игры"}
            },
        }
    )
    assert ev == IncomingMessage("2000000001", "10", "Игры", is_chat=True, mentioned=True)


def test_parse_chat_without_mention_and_from_community():
    ch = channel()
    ev = ch.parse_update(
        {
            "type": "message_new",
            "object": {"message": {"peer_id": 2000000001, "from_id": 10, "text": "просто разговор"}},
        }
    )
    assert isinstance(ev, IncomingMessage) and not ev.mentioned
    assert (
        ch.parse_update(
            {
                "type": "message_new",
                "object": {"message": {"peer_id": 2000000001, "from_id": -GROUP, "text": "x"}},
            }
        )
        is None
    )


def test_parse_start_with_ref_and_button_and_allow():
    ch = channel()
    start = ch.parse_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "peer_id": 10,
                    "from_id": 10,
                    "text": "Начать",
                    "ref": "chat",
                    "payload": '{"command":"start"}',
                }
            },
        }
    )
    assert start.ref == "chat" and start.payload == {"command": "start"} and not start.is_chat

    press = ch.parse_update(
        {
            "type": "message_event",
            "object": {
                "user_id": 10,
                "peer_id": 2000000001,
                "event_id": "e1",
                "payload": {"m": 5, "v": "going"},
                "conversation_message_id": 17,
            },
        }
    )
    assert press == ButtonPress(
        "2000000001", "10", MessageRef("2000000001", "17"), {"m": 5, "v": "going"}, "e1"
    )

    assert ch.parse_update({"type": "message_allow", "object": {"user_id": 10, "key": ""}}) == DmAllowed(
        "10", True
    )
    assert ch.parse_update({"type": "group_join", "object": {}}) is None


@respx.mock
async def test_send_returns_cmid_and_edit_failure_is_false():
    route = respx.post("https://api.vk.com/method/messages.send").respond(
        json={"response": [{"peer_id": 2000000001, "message_id": 0, "conversation_message_id": 17}]}
    )
    respx.post("https://api.vk.com/method/messages.edit").respond(
        json={"error": {"error_code": 909, "error_msg": "Can't edit this message, because it's too old"}}
    )
    async with httpx.AsyncClient() as client:
        ch = channel(client)
        ref = await ch.send("2000000001", "привет")
        assert ref == MessageRef("2000000001", "17")
        assert b"peer_ids=2000000001" in route.calls[0].request.content
        assert await ch.edit(ref, "новый текст") is False


@respx.mock
async def test_api_error_raises():
    respx.post("https://api.vk.com/method/users.get").respond(
        json={"error": {"error_code": 5, "error_msg": "auth"}}
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ChannelError) as e:
            await channel(client).user_names(["1"])
    assert e.value.code == 5
