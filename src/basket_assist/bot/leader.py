"""Интерфейс руководителя команды в личке: сопоставление и передача прав и. о. капитана.

Кнопки без состояния: всё нужное для следующего шага — в payload (`a` — действие).
Ограничения VK для inline-клавиатуры: ≤ 10 кнопок, ≤ 6 рядов, подпись ≤ 40 символов.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.channels.base import Button, ButtonPress, ChannelAdapter
from basket_assist.services import leaders, notify, roster
from basket_assist.storage.tables import (
    Chat,
    League,
    LeagueTeam,
    MessengerAccount,
    Player,
    RosterLink,
    SeasonRosterEntry,
    TeamEntry,
    utcnow,
)

PAGE = 6
DURATIONS = {3: "3 дня", 7: "7 дней", 14: "14 дней", 0: "До отмены"}


def _label(text: str) -> str:
    return text if len(text) <= 40 else text[:39] + "…"


def _fio(r: SeasonRosterEntry) -> str:
    num = f" №{r.number}" if r.number else ""
    return f"{r.last_name} {r.first_name}{num}"


# ---------- Сопоставление ----------


async def matching_summary(session: AsyncSession, tenant_id: int) -> tuple[str, list[list[Button]]]:
    items = await roster.queue(session, tenant_id)
    if not items:
        return "Все игроки сопоставлены с заявками на сезон 👍", []
    sure = await roster.unambiguous(session, tenant_id)
    text = f"На сопоставление: {len(items)} (игрок × лига). Однозначных совпадений: {len(sure)}."
    rows = []
    if sure:
        rows.append([Button(_label(f"Подтвердить однозначные ({len(sure)})"), {"a": "lall"}, "positive")])
    rows.append([Button("Разобрать по одному", {"a": "lnext"}, "primary")])
    return text, rows


async def matching_card(
    session: AsyncSession,
    tenant_id: int,
    after: tuple[int, int] | None = None,
    *,
    same: bool = False,
) -> tuple[str, list[list[Button]]]:
    """Карточка первого элемента очереди или следующего после `after` (игрок, выступление).

    `same=True` — показать сам `after`, если он ещё в очереди (отмена, конфликт).

    Очередь идёт по кругу: после последнего — снова с начала. Поэтому пропущенные
    возвращаются только после всех остальных, а `after` может быть уже решённым элементом.
    """
    items = await roster.queue(session, tenant_id)
    if not items:
        return "Очередь сопоставления пуста.", []
    current = items[0]
    if after is not None:
        player, entry = await session.get(Player, after[0]), await session.get(TeamEntry, after[1])
        if player is not None and entry is not None:
            pos = _queue_key(player, entry.id)
            current = next(
                (i for i in items if (k := _queue_key(i.player, i.entry.id)) > pos or (same and k == pos)),
                items[0],
            )
    text, rows = await _card(session, tenant_id, current)
    return f"{text}\n\nОсталось в очереди: {len(items)}", rows


def _queue_key(player: Player, entry_id: int) -> tuple:
    """Тот же порядок, что в roster.queue: выступление, имя, id."""
    return (entry_id, player.display_name, player.id)


async def item_card(session: AsyncSession, tenant_id: int, player_id: int, entry_id: int):
    """Карточка конкретного участника и лиги — в том числе уже решённого (для «исправить»)."""
    player = await session.get(Player, player_id)
    entry = await session.get(TeamEntry, entry_id)
    item = roster.QueueItem(
        player,
        entry,
        await roster.entry_label(session, entry),
        await roster.candidates(session, tenant_id, player, entry),
    )
    text, rows = await _card(session, tenant_id, item)
    status = await roster.roster_status(session, tenant_id, player_id, entry_id)
    now = {
        "in_roster": "сопоставлен",
        "not_in_roster": "нет в заявке на сезон",
        "not_in_league": "не играет в этой лиге",
        "unmatched": "не сопоставлен",
    }[status]
    if status == "in_roster":
        r = await roster.roster_entry_for(session, tenant_id, player_id, entry_id)
        now += f": {_fio(r)}"
    return f"{text}\n\nСейчас: {now}", rows


async def find_players(session: AsyncSession, tenant_id: int, query: str) -> tuple[str, list[list[Button]]]:
    """«исправить <имя>»: поиск по имени в VK и по ФИО в заявках на сезон, включая «не игроков команды»."""
    q = query.lower().replace("ё", "е").strip()
    players = (
        await session.scalars(select(Player).filter_by(tenant_id=tenant_id).order_by(Player.display_name))
    ).all()
    found = []
    for p in players:
        names = [p.display_name]
        for entry in (await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id))).all():
            if r := await roster.roster_entry_for(session, tenant_id, p.id, entry.id):
                names.append(f"{r.last_name} {r.first_name}")
        if any(q in n.lower().replace("ё", "е") for n in names):
            found.append(p)
    if not found:
        return f"Никого не нашёл по «{query}».", []
    rows = [
        [Button(_label(p.display_name + ("" if p.active else " (не игрок)")), {"a": "lfix", "p": p.id})]
        for p in found[:6]
    ]
    return "Кого исправить?", rows


PLAYERS_MARKS = {"in_roster": "✅", "not_in_roster": "⚠️", "not_in_league": "➖", "unmatched": "❓"}
PLAYERS_LEGEND = (
    "✅ сопоставлен (№ из заявки на сезон) · ⚠️ нет в заявке — нужно дозаявить · "
    "➖ не играет в лиге · ❓ не сопоставлен · 🔕 не открыл личку с ботом"
)
MESSAGE_LIMIT = 3500  # VK режет сообщения длиннее 4096 символов
FIGURE_SPACE = " "  # пробел шириной в цифру — выравнивает номера в пропорциональном шрифте
GAP = FIGURE_SPACE * 2


async def players_list(session: AsyncSession, tenant_id: int) -> list[str]:
    """«игроки»: все игроки команды со статусами по каждой лиге. Список сообщений (длинный — частями)."""
    entries = (
        await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id).order_by(TeamEntry.id))
    ).all()
    leagues = [(e, await _league_name(session, e)) for e in entries]
    players = (
        await session.scalars(
            select(Player).filter_by(tenant_id=tenant_id).order_by(Player.display_name, Player.id)
        )
    ).all()
    dm_open = set(
        (
            await session.scalars(
                select(MessengerAccount.player_id).filter_by(tenant_id=tenant_id, dm_allowed=True)
            )
        ).all()
    )
    active = [p for p in players if p.active]
    # Сначала собираем ячейки (значок, номер), потом выравниваем: в VK шрифт не моноширинный,
    # поэтому ровными делаем только начало строки — значки и номера, добитые пробелом шириной в цифру.
    table: list[tuple[list[tuple[str, str]], str]] = []
    for p in active:
        cells = []
        for entry, _ in leagues:
            status = await roster.roster_status(session, tenant_id, p.id, entry.id)
            number = ""
            if status == "in_roster":
                r = await roster.roster_entry_for(session, tenant_id, p.id, entry.id)
                number = r.number if r is not None and r.number else ""
            cells.append((PLAYERS_MARKS[status], str(number)))
        dm = "" if p.id in dm_open else " 🔕"
        table.append((cells, f"{p.display_name}{dm}"))
    width = max((len(n) for cells, _ in table for _, n in cells), default=0)
    lines = [
        f"Игроки команды: {len(active)}",
        PLAYERS_LEGEND,
        "Колонки слева направо: " + " · ".join(name for _, name in leagues),
        "",
    ]
    for cells, name in table:
        row = GAP.join(mark + FIGURE_SPACE + num.rjust(width, FIGURE_SPACE) for mark, num in cells)
        lines.append(f"{row}{GAP}{name}")
    others = [p.display_name for p in players if not p.active]
    if others:
        lines += ["", f"Не игроки команды ({len(others)}): " + ", ".join(others)]
    lines += ["", "Изменить: «исправить <имя или фамилия>»."]
    return _split(lines)


def _split(lines: list[str]) -> list[str]:
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > MESSAGE_LIMIT:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    return [*chunks, cur] if cur else chunks


async def _league_name(session: AsyncSession, entry: TeamEntry) -> str:
    team = await session.get(LeagueTeam, entry.league_team_id)
    return (await session.get(League, team.league_id)).name


async def player_overview(session: AsyncSession, tenant_id: int, player_id: int):
    player = await session.get(Player, player_id)
    lines = [f"👤 {player.display_name}" + ("" if player.active else " — отмечен «не игрок команды»")]
    rows = []
    for entry in (await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id))).all():
        label = await roster.entry_label(session, entry)
        status = await roster.roster_status(session, tenant_id, player_id, entry.id)
        lines.append(f"• {label}: {status_text(status)}")
        rows.append([Button(_label(f"Изменить: {label}"), {"a": "le", "p": player_id, "e": entry.id})])
    if not player.active:
        rows.append([Button("Вернуть в команду", {"a": "lback", "p": player_id}, "positive")])
    return "\n".join(lines), rows


def status_text(status: str) -> str:
    return {
        "in_roster": "сопоставлен",
        "not_in_roster": "нет в заявке на сезон",
        "not_in_league": "не играет в этой лиге",
        "unmatched": "не сопоставлен",
    }[status]


async def _card(session: AsyncSession, tenant_id: int, item: roster.QueueItem):
    p, e = item.player.id, item.entry.id
    rows: list[list[Button]] = []
    lines = [f"👤 {item.player.display_name} (VK)", f"🏀 {item.league_name}", ""]
    if item.candidates:
        lines.append("Кандидаты из заявки на сезон:")
        for c in item.candidates[:3]:
            r = await session.get(SeasonRosterEntry, c.key)
            mark = "точно" if c.confidence == "exact" else "вероятно"
            lines.append(f"• {_fio(r)} — {mark}")
            rows.append([Button(_label(f"✅ {_fio(r)}"), {"a": "lk", "p": p, "e": e, "r": r.id}, "positive")])
    else:
        lines.append("Совпадений в заявке на сезон не нашлось.")
    rows.append([Button("Выбрать из заявки", {"a": "ls", "p": p, "e": e, "pg": 0}, "primary")])
    rows.append(
        [
            Button("Нет в заявке", {"a": "ln", "p": p, "e": e}),
            Button("Не играет в лиге", {"a": "lnl", "p": p, "e": e}),
        ]
    )
    rows.append(
        [
            Button("Не игрок команды", {"a": "lx", "p": p, "e": e}, "negative"),
            Button("Пропустить", {"a": "lskip", "p": p, "e": e}),
        ]
    )
    return "\n".join(lines), rows


async def roster_page(
    session: AsyncSession, tenant_id: int, player_id: int, entry_id: int, page: int
) -> tuple[str, list[list[Button]]]:
    entry = await session.get(TeamEntry, entry_id)
    player = await session.get(Player, player_id)
    taken = await roster._taken(session, tenant_id, entry)
    rows_all = [r for r in await roster.roster_rows(session, entry) if r.id not in taken]
    chunk = rows_all[page * PAGE : (page + 1) * PAGE]
    buttons = [Button(_label(_fio(r)), {"a": "lk", "p": player_id, "e": entry_id, "r": r.id}) for r in chunk]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    nav = []
    if page > 0:
        nav.append(Button("← Назад", {"a": "ls", "p": player_id, "e": entry_id, "pg": page - 1}))
    if (page + 1) * PAGE < len(rows_all):
        nav.append(Button("Далее →", {"a": "ls", "p": player_id, "e": entry_id, "pg": page + 1}))
    if nav:
        rows.append(nav)
    rows.append([Button("Отмена", {"a": "lcard", "p": player_id, "e": entry_id})])
    label = await roster.entry_label(session, entry)
    pages = max(1, -(-len(rows_all) // PAGE))
    return f"Кто это — {player.display_name}? {label}, стр. {page + 1}/{pages}", rows


# ---------- Передача прав ----------


async def rights_menu(session: AsyncSession, tenant_id: int, page: int = 0) -> tuple[str, list[list[Button]]]:
    grants = await leaders.active_grants(session, tenant_id)
    lines = ["Передача прав капитана (и. о.)."]
    rows: list[list[Button]] = []
    acting = set()
    for i, g in enumerate(grants):
        who = await session.get(Player, g.player_id)
        acting.add(g.player_id)
        until = f"до {g.valid_until:%d.%m}" if g.valid_until else "до отмены"
        lines.append(f"• Сейчас и. о.: {who.display_name}, {until}")
        if i < 2:  # ≤ 2 ряда «Отозвать», чтобы игроки и «Далее →» уместились в 6 рядов VK
            rows.append([Button(_label(f"Отозвать: {who.display_name}"), {"a": "gr", "g": g.id}, "negative")])
    if len(grants) > 2:
        lines.append("(отозвать остальных — после отзыва первых)")
    per_page = 6 - len(rows) - 1  # один ряд оставляем под «Далее →»
    matched = (
        await session.scalars(
            select(Player)
            .join(RosterLink, RosterLink.player_id == Player.id)
            .where(Player.tenant_id == tenant_id, Player.active.is_(True), RosterLink.status == "confirmed")
            .distinct()
            .order_by(Player.display_name)
        )
    ).all()
    matched = [p for p in matched if p.id not in acting]  # действующих и. о. повторно не предлагаем
    chunk = matched[page * per_page : (page + 1) * per_page]
    lines.append("Кому передать? (только сопоставленные игроки)")
    rows += [[Button(_label(p.display_name), {"a": "g1", "p": p.id})] for p in chunk]
    if (page + 1) * per_page < len(matched):
        rows.append([Button("Далее →", {"a": "gm", "pg": page + 1})])
    return "\n".join(lines), rows


async def _announce_in_chat(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, text: str
) -> None:
    chat = (
        (await session.execute(select(Chat).filter_by(tenant_id=tenant_id, channel=channel.channel)))
        .scalars()
        .first()
    )
    if chat is not None:
        await channel.send(chat.peer_id, text)


# ---------- Обработка кнопок ----------


async def on_button(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, me: Player, press: ButtonPress
):
    """Возвращает (text, buttons) для замены карточки или None."""
    a, d = press.payload.get("a"), press.payload
    if not await leaders.is_leader(session, tenant_id, me.id):
        await channel.answer_button(press, "Это доступно только руководителям команды")
        return None

    if a == "lall":
        n = await roster.confirm_all_unambiguous(session, tenant_id, me.id)
        await session.commit()
        await channel.answer_button(press, f"Подтверждено: {n}")
        return await matching_summary(session, tenant_id)
    if a == "lfix":
        await channel.answer_button(press, "")
        return await player_overview(session, tenant_id, d["p"])
    if a == "le":
        await channel.answer_button(press, "")
        return await item_card(session, tenant_id, d["p"], d["e"])
    if a == "lback":
        player = await session.get(Player, d["p"])
        if player is not None and player.tenant_id == tenant_id:
            player.active = True
            await session.commit()
        await channel.answer_button(press, "Возвращён в команду")
        return await player_overview(session, tenant_id, d["p"])
    if a == "lnext":
        await channel.answer_button(press, "")
        return await matching_card(session, tenant_id)
    if a == "lcard":
        await channel.answer_button(press, "")
        return await matching_card(session, tenant_id, after=(d["p"], d["e"]), same=True)
    if a == "lskip":
        await channel.answer_button(press, "Пропущено")
        return await matching_card(session, tenant_id, after=(d["p"], d["e"]))
    if a == "ls":
        await channel.answer_button(press, "Заявка на сезон")
        return await roster_page(session, tenant_id, d["p"], d["e"], d.get("pg", 0))
    if a == "lk":
        try:
            await roster.confirm(session, tenant_id, d["p"], d["e"], d["r"], me.id)
        except roster.MatchConflict:
            await channel.answer_button(press, "Эта строка уже связана с другим игроком")
            return await matching_card(session, tenant_id, after=(d["p"], d["e"]), same=True)
        await session.commit()
        await channel.answer_button(press, "Связь подтверждена")
        return await matching_card(session, tenant_id, after=(d["p"], d["e"]))
    if a == "ln":
        await roster.mark_not_in_roster(session, tenant_id, d["p"], d["e"], me.id)
        await session.commit()
        entry = await session.get(TeamEntry, d["e"])
        label = await roster.entry_label(session, entry)
        # Игроку здесь не пишем: его предупреждают, только когда он отметит «Буду» (polls).
        who = await session.get(Player, d["p"])
        for lead in await leaders.leaders(session, tenant_id):
            if lead.id != me.id:
                await notify.dm(
                    session,
                    channel,
                    tenant_id,
                    lead.id,
                    f"Отметка от {me.display_name}: {who.display_name} — нет в заявке на сезон ({label}). "
                    "Нужно дозаявить.",
                )
        await channel.answer_button(press, "Отмечено: нет в заявке")
        return await matching_card(session, tenant_id, after=(d["p"], d["e"]))
    if a == "lnl":  # дозаявлять не нужно — без уведомлений
        await roster.mark_not_in_league(session, tenant_id, d["p"], d["e"], me.id)
        await session.commit()
        await channel.answer_button(press, "Отмечено: не играет в этой лиге")
        return await matching_card(session, tenant_id, after=(d["p"], d["e"]))
    if a == "lx":
        await roster.mark_not_team_player(session, tenant_id, d["p"])
        await session.commit()
        await channel.answer_button(press, "Отмечено: не игрок команды")
        return await matching_card(session, tenant_id, after=(d["p"], d["e"]))

    # передача прав — только капитан и заместитель
    if a in ("gm", "g1", "g2", "g3", "gr") and not await leaders.is_captain_or_vice(session, me.id):
        await channel.answer_button(press, "Передавать права может только капитан или заместитель")
        return None
    if a == "gm":
        await channel.answer_button(press, "")
        return await rights_menu(session, tenant_id, d.get("pg", 0))
    if a == "g1":
        who = await session.get(Player, d["p"])
        await channel.answer_button(press, "Выберите срок")
        rows = [
            [
                Button(label, {"a": "g2", "p": d["p"], "d": days})
                for days, label in list(DURATIONS.items())[i : i + 2]
            ]
            for i in (0, 2)
        ]
        return f"Передать права капитана: {who.display_name}. На какой срок?", rows
    if a == "g2":
        await channel.answer_button(press, "Ещё вопрос")
        rows = [
            [Button("Да, не присылать", {"a": "g3", "p": d["p"], "d": d["d"], "s": 1})],
            [Button("Нет, присылать", {"a": "g3", "p": d["p"], "d": d["d"], "s": 0})],
        ]
        return "Не присылать вам сводки на это время?", rows
    if a == "g3":
        days = int(d["d"])
        until = utcnow() + timedelta(days=days) if days else None
        try:
            await leaders.grant_acting(session, tenant_id, me.id, d["p"], until, bool(d.get("s")))
        except leaders.PermissionDenied as e:
            await channel.answer_button(press, str(e))
            return None
        await session.commit()
        who = await session.get(Player, d["p"])
        until_text = f"до {until:%d.%m}" if until else "до отмены"
        await notify.dm(
            session,
            channel,
            tenant_id,
            who.id,
            f"Вам переданы права капитана {until_text}: сводки, заявка на игру, сопоставление.",
        )
        await _announce_in_chat(
            session,
            channel,
            tenant_id,
            f"{until_text.capitalize()} обязанности капитана исполняет {who.display_name}.",
        )
        await channel.answer_button(press, "Права переданы")
        return await rights_menu(session, tenant_id)
    if a == "gr":
        try:
            grant = await leaders.revoke(session, tenant_id, d["g"], me.id)
        except leaders.PermissionDenied as e:
            await channel.answer_button(press, str(e))
            return None
        await session.commit()
        await notify.dm(session, channel, tenant_id, grant.player_id, "Передача прав капитана отменена.")
        await channel.answer_button(press, "Права отозваны")
        return await rights_menu(session, tenant_id)
    return None
