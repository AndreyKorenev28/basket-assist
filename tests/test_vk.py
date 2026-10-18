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


# ---------- обращение, набранное вручную, и ответ на сообщение бота ----------

NAME, SCREEN = "Basket Assist", "basket_helper_bot"  # имя и короткий адрес тестового сообщества


def _msg(text: str, peer: int = 2000000001, **extra) -> dict:
    return {
        "type": "message_new",
        "object": {"message": {"peer_id": peer, "from_id": 10, "text": text, **extra}},
    }


def named_channel(client=None) -> VkChannel:
    ch = channel(client)
    ch._set_plain_mention([NAME, SCREEN])
    return ch


@pytest.mark.parametrize(
    ("text", "rest"),
    [
        ("@Basket Assist игры", "игры"),
        ("@basket assist игры", "игры"),
        ("@BASKET ASSIST, игры", "игры"),
        ("@Basket_Assist игры", "игры"),
        ("@basketassist игры", "игры"),
        ("@Basket   Assist: игры", "игры"),
        ("  @Basket Assist игры", "игры"),
        ("@Basket Assist", ""),
        ("@Basket Assist?", "?"),
        ("@Basket Assist!", "!"),
        ("@Basket Assist.", ""),  # точка — разделитель, как «:» и «;» → помощь
        ("@Basket Assist; игры", "игры"),
        ("@Basket Assist.: игры", "игры"),
        ("@Basket Assist\nигры", "игры"),
        (f"@{SCREEN} игры", "игры"),
        (f"@{SCREEN.upper()}, игры", "игры"),
        (f"@club{GROUP} игры", "игры"),
        (f"@public{GROUP} игры", "игры"),
        (f"@CLUB{GROUP}", ""),
    ],
)
def test_plain_mention_at_start(text, rest):
    ev = named_channel().parse_update(_msg(text))
    assert ev == IncomingMessage("2000000001", "10", rest, is_chat=True, mentioned=True)


@pytest.mark.parametrize(
    "text",
    [
        "привет @basket assist",
        "привет @Basket Assist игры",
        "привет\n@Basket Assist игры",
        "@basket assistant игры",
        "@basket assistant",
        "@Basket Assistx",
        "@Basket-Assist игры",
        f"@club{GROUP}0 игры",
        f"@club{GROUP + 1} игры",
        f"@{SCREEN}s",
        "Basket Assist игры",
        "@ Basket Assist игры",
        "@Basket Assist_ игры",
    ],
)
def test_plain_mention_not_counted(text):
    ev = named_channel().parse_update(_msg(text))
    assert isinstance(ev, IncomingMessage) and not ev.mentioned
    assert ev.text == text.strip()


def test_plain_mention_by_name_needs_load_names():
    """До load_names работает только @club<ID>/@public<ID>; [club…|…] — как раньше."""
    ch = channel()
    assert not ch.parse_update(_msg("@Basket Assist игры")).mentioned
    assert ch.parse_update(_msg(f"@club{GROUP} игры")).mentioned
    ev = named_channel().parse_update(_msg(f"[club{GROUP}|@{SCREEN}], игры"))
    assert ev.mentioned and ev.text == "игры"
    ev = named_channel().parse_update(_msg(f"[public{GROUP}|Basket Assist] игры"))
    assert ev.mentioned and ev.text == "игры"


def test_plain_mention_cut_only_once_at_start():
    ev = named_channel().parse_update(_msg("@Basket Assist @Basket Assist игры"))
    assert ev.mentioned and ev.text == "@Basket Assist игры"


def test_plain_mention_in_dm_is_cut_too():
    ev = named_channel().parse_update(_msg("@Basket Assist помощь", peer=10))
    assert ev == IncomingMessage("10", "10", "помощь", is_chat=False, mentioned=True)


def test_reply_to_bot_is_mention():
    ch = channel()
    ev = ch.parse_update(_msg("игры", reply_message={"from_id": -GROUP, "text": "анонс"}))
    assert ev == IncomingMessage("2000000001", "10", "игры", is_chat=True, mentioned=True)
    for reply in ({"from_id": 10, "text": "x"}, {"from_id": -(GROUP + 1), "text": "x"}, {}, None):
        ev = ch.parse_update(_msg("игры", reply_message=reply))
        assert not ev.mentioned, reply


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        {"groups": [{"id": GROUP, "name": NAME, "screen_name": SCREEN}], "profiles": []},  # 5.199
        [{"id": GROUP, "name": NAME, "screen_name": SCREEN}],  # старый формат — просто список
    ],
)
async def test_load_names_formats(response):
    route = respx.post("https://api.vk.com/method/groups.getById").respond(json={"response": response})
    async with httpx.AsyncClient() as client:
        ch = channel(client)
        await ch.load_names()
    assert f"group_id={GROUP}".encode() in route.calls[0].request.content
    for text in ("@basket assist игры", f"@{SCREEN} игры", f"@club{GROUP} игры"):
        ev = ch.parse_update(_msg(text))
        assert ev.mentioned and ev.text == "игры", text


@respx.mock
async def test_load_names_without_screen_name_and_empty():
    respx.post("https://api.vk.com/method/groups.getById").respond(
        json={"response": {"groups": [{"id": GROUP, "name": NAME}], "profiles": []}}
    )
    async with httpx.AsyncClient() as client:
        ch = channel(client)
        await ch.load_names()
        assert ch.parse_update(_msg("@Basket Assist игры")).mentioned
    respx.post("https://api.vk.com/method/groups.getById").respond(
        json={"response": {"groups": [], "profiles": []}}
    )
    async with httpx.AsyncClient() as client:
        ch = channel(client)
        await ch.load_names()
        assert not ch.parse_update(_msg("@Basket Assist игры")).mentioned
        assert ch.parse_update(_msg(f"@club{GROUP} игры")).mentioned


@respx.mock
@pytest.mark.parametrize(
    "fail",
    [
        {"json": {"error": {"error_code": 5, "error_msg": "auth"}}},
        {"side_effect": httpx.ConnectError("нет сети")},
        {"side_effect": httpx.ReadTimeout("таймаут")},
    ],
)
async def test_load_names_failure_is_not_fatal(fail, caplog):
    route = respx.post("https://api.vk.com/method/groups.getById")
    if "json" in fail:
        route.respond(json=fail["json"])
    else:
        route.side_effect = fail["side_effect"]
    async with httpx.AsyncClient() as client:
        ch = channel(client)
        with caplog.at_level("WARNING"):
            await ch.load_names()  # не падает
    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert ch.parse_update(_msg(f"@club{GROUP} игры")).mentioned
    assert not ch.parse_update(_msg("@Basket Assist игры")).mentioned


@respx.mock
async def test_load_names_failure_keeps_previous_names():
    respx.post("https://api.vk.com/method/groups.getById").respond(
        json={"error": {"error_code": 10, "error_msg": "internal"}}
    )
    async with httpx.AsyncClient() as client:
        ch = named_channel(client)
        await ch.load_names()
    assert ch.parse_update(_msg("@Basket Assist игры")).mentioned


def _long_poll(update: dict) -> None:
    respx.post("https://api.vk.com/method/groups.getLongPollServer").respond(
        json={"response": {"server": "https://lp.vk.test/check", "key": "k", "ts": "1"}}
    )
    respx.get("https://lp.vk.test/check").respond(json={"ts": "2", "updates": [update]})


async def _first_event(ch: VkChannel):
    stream = ch.events()
    try:
        return await stream.__anext__()
    finally:
        await stream.aclose()


@respx.mock
async def test_events_loads_names_first():
    names = respx.post("https://api.vk.com/method/groups.getById").respond(
        json={"response": {"groups": [{"id": GROUP, "name": NAME, "screen_name": SCREEN}], "profiles": []}}
    )
    _long_poll(_msg("@Basket Assist, игры"))
    async with httpx.AsyncClient() as client:
        ev = await _first_event(channel(client))
    assert names.called
    assert ev == IncomingMessage("2000000001", "10", "игры", is_chat=True, mentioned=True)


@respx.mock
async def test_events_work_when_names_unavailable():
    respx.post("https://api.vk.com/method/groups.getById").mock(side_effect=httpx.ConnectError("нет сети"))
    _long_poll(_msg(f"@club{GROUP} игры"))
    async with httpx.AsyncClient() as client:
        ev = await _first_event(channel(client))
    assert ev.mentioned and ev.text == "игры"


# ---------- ответ на нажатие кнопки ----------


@respx.mock
@pytest.mark.parametrize(
    ("toast", "link", "expected"),
    [
        ("", "https://vk.me/club1?ref=chat", '{"type": "open_link", "link": "https://vk.me/club1?ref=chat"}'),
        (
            "Готово",
            "https://vk.me/club1?ref=chat",
            '{"type": "open_link", "link": "https://vk.me/club1?ref=chat"}',
        ),
        ("Готово", None, '{"type": "show_snackbar", "text": "Готово"}'),
        ("", None, None),
    ],
)
async def test_answer_button_event_data(toast, link, expected):
    route = respx.post("https://api.vk.com/method/messages.sendMessageEventAnswer").respond(
        json={"response": 1}
    )
    press = ButtonPress("2000000001", "10", MessageRef("2000000001", "5"), {"c": "бот"}, event_id="ev1")
    async with httpx.AsyncClient() as client:
        await channel(client).answer_button(press, toast, link=link)
    form = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert form["event_id"] == "ev1" and form["user_id"] == "10" and form["peer_id"] == "2000000001"
    assert form.get("event_data") == expected
