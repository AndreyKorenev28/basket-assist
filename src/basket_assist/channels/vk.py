"""Канал VK: тонкий клиент VK API на httpx + Bots Long Poll (ADR 0005).

Проверено в spikes/vk-probe: message_event с user_id, sendMessageEventAnswer, messages.edit по cmid,
messages.pin и getConversationMembers — только при правах администратора беседы (ошибки 925 / 917).
"""

import asyncio
import json
import logging
import random
import re
from collections.abc import AsyncIterator

import httpx

from basket_assist.channels.base import (
    Button,
    ButtonPress,
    ChannelError,
    DmAllowed,
    Event,
    IncomingMessage,
    MessageRef,
)

log = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method"
API_VERSION = "5.199"
CHAT_PEER_BASE = 2_000_000_000


class VkClient:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self._token = token
        self._client = client

    async def call(self, method: str, **params) -> object:
        data = {
            k: (json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else v)
            for k, v in params.items()
            if v is not None
        }
        data.update(access_token=self._token, v=API_VERSION)
        r = await self._client.post(f"{API_URL}/{method}", data=data)
        body = r.json()
        if "error" in body:
            err = body["error"]
            raise ChannelError(err.get("error_code"), f"{method}: {err.get('error_msg')}")
        return body["response"]


def keyboard(buttons: list[list[Button]] | None) -> dict | None:
    if not buttons:
        return None
    return {
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "callback",
                        "label": b.label,
                        "payload": json.dumps(b.payload, ensure_ascii=False),
                    },
                    "color": b.color,
                }
                for b in row
            ]
            for row in buttons
        ],
    }


def _payload(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


class VkChannel:
    channel = "vk"

    def __init__(self, client: VkClient, group_id: int):
        self._api = client
        self._group_id = group_id
        self._mention = re.compile(rf"\[(club|public){group_id}\|[^\]]*\]\s*,?\s*")

    # ---------- входящие ----------

    def parse_update(self, update: dict) -> Event | None:
        kind, obj = update.get("type"), update.get("object") or {}
        if kind == "message_new":
            msg = obj.get("message") or {}
            peer_id = msg.get("peer_id")
            from_id = msg.get("from_id")
            if peer_id is None or from_id is None or from_id < 0:  # сообщения от сообществ игнорируем
                return None
            text = msg.get("text") or ""
            is_chat = peer_id > CHAT_PEER_BASE
            mentioned = bool(self._mention.search(text))
            return IncomingMessage(
                peer_id=str(peer_id),
                user_id=str(from_id),
                text=self._mention.sub("", text).strip(),
                is_chat=is_chat,
                mentioned=mentioned,
                ref=msg.get("ref"),
                payload=_payload(msg.get("payload")),
                action=(msg.get("action") or {}).get("type"),
                action_member_id=(
                    str(msg["action"]["member_id"]) if (msg.get("action") or {}).get("member_id") else None
                ),
            )
        if kind == "message_event":
            return ButtonPress(
                peer_id=str(obj["peer_id"]),
                user_id=str(obj["user_id"]),
                message=MessageRef(str(obj["peer_id"]), str(obj["conversation_message_id"])),
                payload=_payload(obj.get("payload")),
                event_id=obj["event_id"],
            )
        if kind in ("message_allow", "message_deny"):
            return DmAllowed(user_id=str(obj["user_id"]), allowed=kind == "message_allow")
        return None

    async def events(self) -> AsyncIterator[Event]:
        """Bots Long Poll. Сетевые сбои — повтор с паузой; failed=1 — новый ts; 2/3 — новый сервер."""
        server = await self._api.call("groups.getLongPollServer", group_id=self._group_id)
        backoff = 1
        async with httpx.AsyncClient(timeout=35) as lp:
            while True:
                try:
                    r = await lp.get(
                        server["server"],
                        params={"act": "a_check", "key": server["key"], "ts": server["ts"], "wait": 25},
                    )
                    data = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("long poll: %s, повтор через %s с", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 1
                if "failed" in data:
                    if data["failed"] == 1:
                        server["ts"] = data["ts"]
                    else:
                        server = await self._api.call("groups.getLongPollServer", group_id=self._group_id)
                    continue
                server["ts"] = data["ts"]
                for update in data.get("updates", []):
                    event = self.parse_update(update)
                    if event is not None:
                        yield event

    # ---------- исходящие ----------

    async def send(self, peer_id: str, text: str, buttons: list[list[Button]] | None = None) -> MessageRef:
        # peer_ids (а не peer_id) — чтобы в ответе был conversation_message_id
        resp = await self._api.call(
            "messages.send",
            peer_ids=peer_id,
            message=text,
            random_id=random.randint(1, 2**31),
            keyboard=keyboard(buttons),
            disable_mentions=1,
        )
        item = resp[0]
        if "error" in item:
            raise ChannelError(
                item["error"].get("code"), f"messages.send: {item['error'].get('description')}"
            )
        return MessageRef(str(item["peer_id"]), str(item["conversation_message_id"]))

    async def edit(self, msg: MessageRef, text: str, buttons: list[list[Button]] | None = None) -> bool:
        try:
            await self._api.call(
                "messages.edit",
                peer_id=msg.peer_id,
                conversation_message_id=msg.message_id,
                message=text,
                keyboard=keyboard(buttons),
                disable_mentions=1,
            )
        except ChannelError as e:
            log.warning("не удалось отредактировать %s: %s", msg, e)
            return False
        return True

    async def pin(self, msg: MessageRef) -> bool:
        try:
            await self._api.call("messages.pin", peer_id=msg.peer_id, conversation_message_id=msg.message_id)
        except ChannelError as e:
            log.warning("не удалось закрепить %s: %s", msg, e)
            return False
        return True

    async def answer_button(self, press: ButtonPress, toast: str) -> None:
        await self._api.call(
            "messages.sendMessageEventAnswer",
            event_id=press.event_id,
            user_id=press.user_id,
            peer_id=press.peer_id,
            event_data={"type": "show_snackbar", "text": toast[:90]}
            if toast
            else None,  # пусто — просто подтвердить
        )

    async def chat_members(self, peer_id: str) -> list[str]:
        resp = await self._api.call("messages.getConversationMembers", peer_id=peer_id)
        return [str(i["member_id"]) for i in resp.get("items", []) if i.get("member_id", 0) > 0]

    async def user_names(self, user_ids: list[str]) -> dict[str, str]:
        if not user_ids:
            return {}
        resp = await self._api.call("users.get", user_ids=",".join(user_ids))
        return {str(u["id"]): f"{u['first_name']} {u['last_name']}".strip() for u in resp}

    def connect_link(self, ref: str) -> str:
        return f"https://vk.me/club{self._group_id}?ref={ref}"
