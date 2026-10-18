"""Проверка возможностей бота VK на тестовой беседе (одноразовый скрипт, не код сервиса).

Только стандартная библиотека Python. Настройки — переменные окружения или файл .env рядом:
    VK_GROUP_TOKEN=...   ключ доступа сообщества (права: messages, manage)
    VK_GROUP_ID=...      числовой ID сообщества (без минуса)

Запуск:  py probe.py
Все входящие события и ответы API пишутся в events.jsonl рядом со скриптом.
Команды в беседе (упомяните бота, например «@club123 пост»): пост, участники, закреп, лс, помощь.
"""
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API_VERSION = "5.199"
HERE = Path(__file__).resolve().parent
LOG = HERE / "events.jsonl"

VOTES = {"going": "Буду", "not_going": "Не буду"}
votes: dict[int, dict[int, str]] = {}  # conversation_message_id -> {user_id: vote}
last_post: dict[int, int] = {}          # peer_id -> conversation_message_id последнего анонса
names: dict[int, str] = {}              # user_id -> «Имя Фамилия» (кэш users.get)


def load_env() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def log(kind: str, data) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%H:%M:%S"), "kind": kind, "data": data}, ensure_ascii=False) + "\n")


def http_get(url: str, params: dict, timeout: int = 35) -> dict:
    with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def api(method: str, **params):
    params = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
    params.update(access_token=TOKEN, v=API_VERSION)
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.vk.com/method/{method}", data=data, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    log(f"api:{method}", {"params": {k: v for k, v in params.items() if k != "access_token"}, "resp": resp})
    if "error" in resp:
        e = resp["error"]
        print(f"  ✗ {method}: [{e.get('error_code')}] {e.get('error_msg')}")
        return None
    print(f"  ✓ {method}")
    return resp["response"]


def keyboard() -> dict:
    return {
        "inline": True,
        "buttons": [[
            {"action": {"type": "callback", "label": label, "payload": json.dumps({"vote": key})},
             "color": {"going": "positive", "not_going": "negative"}[key]}
            for key, label in VOTES.items()
        ]],
    }


def name_of(user_id: int) -> str:
    if user_id not in names:
        r = api("users.get", user_ids=user_id)
        names[user_id] = f"{r[0]['first_name']} {r[0]['last_name']}" if r else f"id{user_id}"
    return names[user_id]


def post_text(cmid: int | None) -> str:
    v = votes.get(cmid, {}) if cmid else {}
    lines = ["ТЕСТ. Матч: Westchester — Соперник, сб 12:00, Шелепиха.", "Кто идёт?", ""]
    for key, label in VOTES.items():
        who = [name_of(u) for u, x in v.items() if x == key]
        lines.append(f"{label} ({len(who)}): {', '.join(who) if who else '—'}")
    lines.append(f"\nОбновлено: {time.strftime('%d.%m %H:%M:%S')}")
    return "\n".join(lines)


def send(peer_id: int, text: str, **extra):
    # peer_ids (а не peer_id) — чтобы в ответе получить conversation_message_id
    return api("messages.send", peer_ids=peer_id, message=text, random_id=random.randint(1, 2**31), **extra)


def on_command(peer_id: int, from_id: int, cmd: str) -> None:
    print(f"→ команда «{cmd}» от {from_id} в {peer_id}")
    if cmd == "пост":
        r = send(peer_id, post_text(None), keyboard=keyboard())
        if r and isinstance(r, list) and r[0].get("conversation_message_id"):
            cmid = r[0]["conversation_message_id"]
            last_post[peer_id] = cmid
            votes[cmid] = {}
    elif cmd == "участники":
        r = api("messages.getConversationMembers", peer_id=peer_id, fields="first_name,last_name")
        if r:
            names = [f"{p['first_name']} {p['last_name']}" for p in r.get("profiles", [])]
            send(peer_id, f"Участников: {r.get('count')}. Профили: {', '.join(names) or '—'}")
    elif cmd.startswith("обнови"):
        # «обнови <cmid>» — проверка редактирования старого анонса (этап В)
        parts = cmd.split()
        cmid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else last_post.get(peer_id)
        if cmid:
            api("messages.edit", peer_id=peer_id, conversation_message_id=cmid, message=post_text(cmid), keyboard=keyboard())
    elif cmd == "закреп":
        if peer_id in last_post:
            api("messages.pin", peer_id=peer_id, conversation_message_id=last_post[peer_id])
        else:
            send(peer_id, "Сначала «пост».")
    elif cmd == "лс":
        allowed = api("messages.isMessagesFromGroupAllowed", group_id=GROUP_ID, user_id=from_id)
        send(peer_id, f"isMessagesFromGroupAllowed = {allowed}. Пробую написать в личку…")
        send(from_id, "Тест: личное сообщение от бота.")
        link = f"https://vk.me/club{GROUP_ID}?ref=probe"  # ⚠ формат ссылки по ID проверяется этим тестом
        send(peer_id, f"Ссылка для подключения: {link}")
    else:
        send(peer_id, "Команды: пост, участники, закреп, лс")


def on_event(upd: dict) -> None:
    t, obj = upd.get("type"), upd.get("object", {})
    log(f"event:{t}", obj)
    print(f"• событие {t}")
    if t == "message_new":
        msg = obj.get("message", {})
        peer_id, from_id, text = msg.get("peer_id"), msg.get("from_id"), msg.get("text", "")
        if msg.get("action"):
            print(f"  action: {msg['action']}")
        if msg.get("ref"):
            print(f"  ref: {msg['ref']}  ref_source: {msg.get('ref_source')}")
        cmd = re.sub(r"\[club\d+\|[^\]]*\]", "", text).strip(" ,").lower()
        mentioned = re.search(rf"\[club{GROUP_ID}\|", text) is not None
        if peer_id and peer_id > 2_000_000_000:
            if cmd and mentioned:  # с правами админа бот видит всё — реагируем только на обращения к нему
                on_command(peer_id, from_id, cmd)
        elif peer_id:
            if cmd in ("пост", "лс"):  # кнопки и личка проверяются и в личном диалоге
                on_command(peer_id, from_id, cmd)
            else:
                send(peer_id, f"Привет! Получил: «{text}» (ref={msg.get('ref')}). Личка работает. Команды: пост, лс")
    elif t == "message_event":
        payload = obj.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        vote, user_id, peer_id, cmid = payload.get("vote"), obj.get("user_id"), obj.get("peer_id"), obj.get("conversation_message_id")
        print(f"  нажатие: user {user_id} → {vote} (cmid {cmid})")
        votes.setdefault(cmid, {})[user_id] = vote
        api("messages.sendMessageEventAnswer", event_id=obj.get("event_id"), user_id=user_id, peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": f"Отмечено: {VOTES.get(vote, vote)}"})
        api("messages.edit", peer_id=peer_id, conversation_message_id=cmid, message=post_text(cmid), keyboard=keyboard())


def main() -> None:
    print(f"Бот сообщества {GROUP_ID} запущен. Лог: {LOG}. Ctrl+C — выход.")
    lp = api("groups.getLongPollServer", group_id=GROUP_ID)
    if not lp:
        sys.exit("Не удалось получить Long Poll сервер: проверьте токен и что Long Poll API включён в настройках сообщества.")
    while True:
        try:
            r = http_get(lp["server"], {"act": "a_check", "key": lp["key"], "ts": lp["ts"], "wait": 25})
        except Exception as e:  # сетевые сбои — просто повторяем
            print(f"  сеть: {e}")
            time.sleep(3)
            continue
        if "failed" in r:
            if r["failed"] == 1:
                lp["ts"] = r["ts"]
            else:
                lp = api("groups.getLongPollServer", group_id=GROUP_ID) or lp
            continue
        lp["ts"] = r["ts"]
        for upd in r.get("updates", []):
            try:
                on_event(upd)
            except Exception as e:
                print(f"  ошибка обработки: {e}")
                log("error", {"update": upd, "error": repr(e)})


if __name__ == "__main__":
    load_env()
    TOKEN = os.environ.get("VK_GROUP_TOKEN") or sys.exit("Нет VK_GROUP_TOKEN (переменная окружения или .env)")
    GROUP_ID = int(os.environ.get("VK_GROUP_ID") or sys.exit("Нет VK_GROUP_ID"))
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
