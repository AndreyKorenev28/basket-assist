"""Интерфейс руководителя команды в личке: сопоставление и передача прав и. о. капитана.

Кнопки без состояния: всё нужное для следующего шага — в payload (`a` — действие).
Ограничения VK для inline-клавиатуры: ≤ 10 кнопок, ≤ 6 рядов, подпись ≤ 40 символов.
"""

import json
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.channels.base import Button, ButtonPress, ChannelAdapter
from basket_assist.domain.models import ChangeKind, TimeStatus
from basket_assist.services import chats, digests, leaders, notify, polls, roster, weekly
from basket_assist.services.schedule import format_when, teams, upcoming_matches, weekend_days
from basket_assist.services.table_image import PlayersTable, TableRow, render_players
from basket_assist.storage.tables import (
    ApplicationEntry,
    ChangeEvent,
    League,
    LeagueTeam,
    Match,
    MessengerAccount,
    Player,
    RosterLink,
    SeasonRosterEntry,
    TeamEntry,
    utcnow,
)

PAGE = 4  # строк заявки на сезон на странице: по одной ФИО в ряд + навигация + «Отмена» ≤ 6 рядов VK
DURATIONS = {3: "3 дня", 7: "7 дней", 14: "14 дней", 0: "До отмены"}


def _label(text: str) -> str:
    return text if len(text) <= 40 else text[:39] + "…"


def _fio(r: SeasonRosterEntry) -> str:
    num = f" №{r.number}" if r.number else ""
    return f"{r.last_name} {r.first_name}{num}"


# ---------- Состав на матч ----------


async def lineup(session: AsyncSession, tenant_id: int) -> list[tuple[str, list[list[Button]] | None]]:
    """«Состав и заявка»: по сообщению на каждую лигу (ближайший матч).

    Идёт опрос — карточка с правкой заявки на игру и «📋 Текст» (текст для копирования);
    опроса нет — состояние матча без кнопок и подсказка, что делать капитану.
    """
    out: list[tuple[str, list[list[Button]] | None]] = []
    for league, ours, rival, match in await digests.nearest_by_league(session, tenant_id):
        if match is None:
            out.append((digests.no_matches_block(league, ours), None))
        elif match.start_at is not None and await polls._current_poll(session, tenant_id, match.id):
            out.extend(await weekly.application_card(session, tenant_id, match))
        else:
            text = await digests._match_block(session, tenant_id, league, ours, rival, match)
            if match.start_at is None:
                text += f"\nЗнаете время — напишите «время {match.match_date:%d.%m} 19:00»."
            else:
                text += f" Он запускается в среду перед игрой, в {league.poll_time}."
            out.append((text, None))
    return out or [(digests.NO_MATCHES, None)]


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


async def number_command(session: AsyncSession, tenant_id: int, args: str) -> tuple[str, list[list[Button]]]:
    """«номер Петров 23 [лига]»: номер для заявки на игру вместо номера из заявки на сезон (по лиге)."""
    words = args.split()
    pos = next((i for i, w in enumerate(words) if w.isdigit() and len(w) <= 3), None)
    if pos is None or pos == 0:
        return (
            "Напишите так: «номер Петров 23». Если игрок в двух лигах — можно добавить лигу: «номер Петров 23 АБЛ».",
            [],
        )
    query, number, league_q = " ".join(words[:pos]), words[pos], " ".join(words[pos + 1 :]).lower()
    q = query.lower().replace("ё", "е")
    found = []  # (имя, лига, player_id, entry_id) — только заявленные на сезон
    for entry in (
        await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id).order_by(TeamEntry.id))
    ).all():
        league = await _league_name(session, entry)
        if league_q and league_q not in league.lower():
            continue
        for p in (await session.scalars(select(Player).filter_by(tenant_id=tenant_id, active=True))).all():
            r = await roster.roster_entry_for(session, tenant_id, p.id, entry.id)
            if r is None:
                continue
            name = f"{r.last_name} {r.first_name}"
            if q in name.lower().replace("ё", "е") or q in p.display_name.lower().replace("ё", "е"):
                found.append((name, league, p.id, entry.id))
    if not found:
        return f"Не нашёл «{query}» среди заявленных на сезон" + (
            f" в лиге «{league_q}»" if league_q else ""
        ) + ".", []
    if len(found) == 1:
        name, league, pid, eid = found[0]
        await set_number(session, tenant_id, pid, eid, number)
        return f"Номер записан: {name} — №{number} ({league}). В заявку на игру пойдёт он.", []
    if len(found) > 6:
        return f"Нашлось много игроков по «{query}» — уточните фамилию.", []
    rows = [
        [Button(_label(f"{name} — {league}"), {"a": "num", "p": pid, "e": eid, "n": number})]
        for name, league, pid, eid in found
    ]
    return f"Кому записать №{number}?", rows


TIME_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\s+(\d{1,2})[:.](\d{2})")


async def time_command(session: AsyncSession, tenant_id: int, args: str) -> tuple[str, list[list[Button]]]:
    """«время 25.10 18:30»: время матча, если лига его ещё не назначила (domain.md, «Жизненный цикл матча»)."""
    m = TIME_RE.search(args)
    if m is None:
        return "Напишите так: «время 25.10 18:30» — дата матча и время начала.", []
    day, month, hh, mm = (int(x) for x in m.groups())
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return "Не понял время. Пример: «время 25.10 18:30».", []
    found = [
        (league, ours, rival, match)
        for league, ours, rival, match in await upcoming_matches(
            session,
            tenant_id,
            limit=10,
            include_estimate=True,  # руководитель может опередить сайт лиги
        )
        if (day, month) in _days_of(match)
    ]
    if not found:
        return f"Не нашёл нашего предстоящего матча на {day:02d}.{month:02d}.", []
    hm = f"{hh:02d}:{mm:02d}"
    if len(found) == 1:
        return await set_manual_time(session, tenant_id, found[0][3], hm, (day, month)), []
    rows = [
        [
            Button(
                _label(f"{league.name}: {teams(match, ours, rival)}"),
                {"a": "tm", "m": match.id, "t": hm, "d": f"{day}.{month}"},
            )
        ]
        for league, ours, rival, match in found[:5]
    ]
    return f"В этот день несколько матчей — для какого время {hm}?", rows


def _days_of(match: Match) -> set[tuple[int, int]]:
    """Дни, которыми руководитель может назвать матч: дата матча, а для ожидаемых выходных — сб и вс."""
    days = weekend_days(match) if match.date_is_estimate else (match.match_date,)
    return {(d.day, d.month) for d in days}


async def set_manual_time(
    session: AsyncSession, tenant_id: int, match: Match, hm: str, day_month: tuple[int, int] | None = None
) -> str:
    """Записать время, введённое руководителем. Если время уже есть на сайте лиги — оно главнее."""
    league, ours, rival, _ = await polls._context(session, match, tenant_id)
    if match.start_at is not None and match.time_source != "manual":
        return f"У матча уже есть время с сайта лиги: {format_when(match, league)}. Оно главнее — если лига ошиблась, свяжитесь с ней."
    if match.date_is_estimate:  # дата была ожидаемой (выходные) — руководитель назвал день
        day = next(
            (d for d in weekend_days(match) if day_month is None or (d.day, d.month) == day_month), None
        )
        if day is None:
            return "Этот день не совпадает с выходными матча — напишите дату субботы или воскресенья."
        match.match_date, match.date_is_estimate = day, False
    hh, mm = (int(x) for x in hm.split(":"))
    local = datetime.combine(match.match_date, time(hh, mm), ZoneInfo(league.timezone))
    was, match.start_at = match.start_at, local.astimezone(UTC).replace(tzinfo=None)
    if was is not None and was != match.start_at:  # руководитель поменял своё же время — новый опрос
        session.add(
            ChangeEvent(
                kind=ChangeKind.TIME_CHANGED,
                match_id=match.id,
                payload=json.dumps({"was": was, "now": match.start_at, "source": "manual"}, default=str),
            )
        )
    match.time_status, match.time_source = TimeStatus.TIME_SET, "manual"
    await session.commit()
    if was is not None and was != match.start_at:
        return (
            f"Время изменено: {teams(match, ours, rival)} ({league.name}) — {format_when(match, league)}.\n"
            "Опрос перезапускается: прежние отметки сброшены, игроков попрошу отметиться заново."
        )
    return (
        f"Время записано: {teams(match, ours, rival)} ({league.name}) — {format_when(match, league)}.\n"
        "Опрос запустится сам (в день опроса, а если он уже прошёл — в ближайшую минуту). "
        "Когда лига опубликует время на сайте, бот возьмёт его оттуда."
    )


async def set_number(
    session: AsyncSession, tenant_id: int, player_id: int, entry_id: int, number: str
) -> None:
    link = await roster._link(session, tenant_id, player_id, entry_id)
    if link is None:
        raise ValueError("нет связи с заявкой на сезон")
    link.number_override = number
    await session.commit()


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


PLAYERS_HINT = "Изменить: «исправить <имя или фамилия>»."


async def _players_table(session: AsyncSession, tenant_id: int) -> tuple[PlayersTable, list[str]]:
    """Данные для «игроков» (и текстом, и картинкой) + имена отмеченных «не игрок команды».

    Сопоставленным — ФИО из заявки на сезон (первой лиги, где есть связь), сортировка по фамилии;
    не сопоставленным ни в одной лиге — имя из VK, отдельным блоком в конце.
    """
    entries = (
        await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant_id).order_by(TeamEntry.id))
    ).all()
    leagues = [await _league(session, e) for e in entries]
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
    matched: list[tuple[tuple, TableRow]] = []
    unmatched: list[TableRow] = []
    for p in (p for p in players if p.active):
        cells, fio = [], None
        for entry in entries:
            status = await roster.roster_status(session, tenant_id, p.id, entry.id)
            number = ""
            if status == "in_roster":
                r = await roster.roster_entry_for(session, tenant_id, p.id, entry.id)
                if r is not None:
                    number = str(r.number or "")
                    fio = fio or (r.last_name, r.first_name)
            cells.append((status, number))
        if fio:
            key = (fio[0].lower().replace("ё", "е"), fio[1].lower().replace("ё", "е"), p.id)
            matched.append((key, TableRow(f"{fio[0]} {fio[1]}", cells, p.id in dm_open)))
        else:
            unmatched.append(TableRow(p.display_name, cells, p.id in dm_open))
    matched.sort(key=lambda t: t[0])
    tz = ZoneInfo(leagues[0].timezone) if leagues else UTC  # в MVP у всех лиг Europe/Moscow
    table = PlayersTable(
        leagues=[lg.name for lg in leagues],
        matched=[row for _, row in matched],
        unmatched=unmatched,
        at=datetime.now(UTC).astimezone(tz),
    )
    return table, [p.display_name for p in players if not p.active]


async def players_list(session: AsyncSession, tenant_id: int) -> list[str]:
    """«игроки» текстом: все игроки команды со статусами по каждой лиге. Длинный список — частями.

    В VK шрифт не моноширинный, поэтому ровными делаем только начало строки — значки и номера,
    добитые пробелом шириной в цифру.
    """
    table, others = await _players_table(session, tenant_id)
    rows = table.matched + table.unmatched
    width = max((len(num) for row in rows for _, num in row.cells), default=0)

    def fmt(row: TableRow) -> str:
        cells = GAP.join(
            PLAYERS_MARKS[status] + FIGURE_SPACE + num.rjust(width, FIGURE_SPACE) for status, num in row.cells
        )
        return f"{cells}{GAP}{row.name}" + ("" if row.dm_open else " 🔕")

    lines = [f"Игроки команды: {len(rows)}", PLAYERS_LEGEND, "", _header(table.leagues, width)]
    lines += [fmt(row) for row in table.matched]
    if table.unmatched:
        lines += ["", "Не сопоставлены ни в одной лиге (имя из VK):"]
        lines += [fmt(row) for row in table.unmatched]
    lines += _others_lines(others)
    lines += ["", PLAYERS_HINT]
    return _split(lines)


async def players_image(session: AsyncSession, tenant_id: int) -> tuple[bytes | None, str]:
    """«игроки» картинкой: (PNG или None, если нарисовать не получилось; подпись к картинке)."""
    table, others = await _players_table(session, tenant_id)
    caption = [f"Игроки команды: {len(table.matched) + len(table.unmatched)}", *_others_lines(others)]
    caption += ["", PLAYERS_HINT]
    return render_players(table), "\n".join(caption)


def _others_lines(others: list[str]) -> list[str]:
    return ["", f"Не игроки команды ({len(others)}): " + ", ".join(others)] if others else []


def _header(leagues: list[str], width: int) -> str:
    """Заголовки над колонками. Выравнивание приблизительное: буквы шире цифр, точно в VK не подогнать.

    Ширина ячейки ≈ значок (~1,6 цифры) + пробел + `width` цифр; заглавная буква ≈ 1,25 цифры.
    """
    cell = 2.6 + width
    heads = [name + FIGURE_SPACE * max(0, round(cell - 1.25 * len(name))) for name in leagues]
    return GAP.join(heads) + GAP + "Игрок"


def _split(lines: list[str]) -> list[str]:
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > MESSAGE_LIMIT:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    return [*chunks, cur] if cur else chunks


async def _league(session: AsyncSession, entry: TeamEntry) -> League:
    team = await session.get(LeagueTeam, entry.league_team_id)
    return await session.get(League, team.league_id)


async def _league_name(session: AsyncSession, entry: TeamEntry) -> str:
    return (await _league(session, entry)).name


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
    lines += ["", CARD_LEGEND]
    rows.append([Button("Выбрать из заявки", {"a": "ls", "p": p, "e": e, "pg": 0}, "primary")])
    rows.append(
        [
            Button("Нет в заявке", {"a": "ln", "p": p, "e": e}),
            Button("Не в лиге", {"a": "lnl", "p": p, "e": e}),
        ]
    )
    rows.append(
        [
            Button("Не игрок", {"a": "lx", "p": p, "e": e}, "negative"),
            Button("Пропустить", {"a": "lskip", "p": p, "e": e}),
        ]
    )
    return "\n".join(lines), rows


# подписи кнопок в паре короткие (PAIR_LABEL_MAX) — полный смысл здесь, в тексте карточки
CARD_LEGEND = (
    "«Нет в заявке» — играет за команду, но не заявлен на сезон; «Не в лиге» — не играет в этой лиге; "
    "«Не игрок» — не игрок команды (во всех лигах)."
)


async def roster_page(
    session: AsyncSession, tenant_id: int, player_id: int, entry_id: int, page: int
) -> tuple[str, list[list[Button]]]:
    entry = await session.get(TeamEntry, entry_id)
    player = await session.get(Player, player_id)
    taken = await roster._taken(session, tenant_id, entry)
    rows_all = [r for r in await roster.roster_rows(session, entry) if r.id not in taken]
    chunk = rows_all[page * PAGE : (page + 1) * PAGE]
    # ФИО длинные — по одной в ряд, во всю ширину (PAIR_LABEL_MAX)
    rows = [[Button(_label(_fio(r)), {"a": "lk", "p": player_id, "e": entry_id, "r": r.id})] for r in chunk]
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

RIGHTS_CANCEL = Button("Отмена", {"a": "gx"}, "secondary")


async def rights_menu(session: AsyncSession, tenant_id: int, page: int = 0) -> tuple[str, list[list[Button]]]:
    grants = await leaders.active_grants(session, tenant_id)
    lines = ["Передача прав капитана (и. о.)."]
    rows: list[list[Button]] = []
    acting = set()
    for i, g in enumerate(grants):
        who = await _name(session, tenant_id, g.player_id)
        acting.add(g.player_id)
        until = f"до {g.valid_until:%d.%m}" if g.valid_until else "до отмены"
        lines.append(f"• Сейчас и. о.: {who}, {until}")
        if i < 2:  # ≤ 2 ряда «Отозвать», чтобы игроки и «Далее →» уместились в 6 рядов VK
            rows.append([Button(_label(f"Отозвать: {who}"), {"a": "gr", "g": g.id}, "negative")])
    if len(grants) > 2:
        lines.append("(отозвать остальных — после отзыва первых)")
    per_page = 6 - len(rows) - 1  # один ряд оставляем под «Отмена» и «Далее →»
    matched = (
        await session.scalars(
            select(Player)
            .join(RosterLink, RosterLink.player_id == Player.id)
            .where(Player.tenant_id == tenant_id, Player.active.is_(True), RosterLink.status == "confirmed")
            .distinct()
        )
    ).all()
    # ФИО из заявки на сезон, по фамилии — как в списке «игроки»; действующих и. о. повторно не предлагаем
    named = sorted(
        [(*await roster.team_name(session, tenant_id, p), p.id) for p in matched if p.id not in acting]
    )
    chunk = named[page * per_page : (page + 1) * per_page]
    lines.append("Кому передать? (только сопоставленные игроки)")
    rows += [[Button(_label(name), {"a": "g1", "p": pid})] for _, name, pid in chunk]
    last = [RIGHTS_CANCEL]
    if (page + 1) * per_page < len(named):
        last.append(Button("Далее →", {"a": "gm", "pg": page + 1}))
    rows.append(last)
    return "\n".join(lines), rows


async def _name(session: AsyncSession, tenant_id: int, player_id: int) -> str:
    """ФИО из заявки на сезон (или имя из VK) — для диалога прав и объявления в беседе."""
    return (await roster.team_name(session, tenant_id, await session.get(Player, player_id)))[1]


async def _announce_in_chat(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, text: str
) -> None:
    chat = await chats.active_chat(session, tenant_id, channel.channel)
    if chat is not None:
        await channel.send(chat.peer_id, text)


# ---------- Обработка кнопок ----------


APP_PAGE = 4  # игроков на странице выбора: + «Далее» + «Назад к сводке» ≤ 6 рядов VK


async def _application_button(
    session: AsyncSession,
    channel: ChannelAdapter,
    tenant_id: int,
    me: Player,
    press: ButtonPress,
    match: Match,
    d,
):
    """Кнопки под сводкой: обновить; добавить в заявку на игру / убрать из неё (domain.md, «Заявка на игру»)."""
    await polls._context(session, match, tenant_id)  # чужой матч → PollError до любых записей и ответов
    a, kind = d["a"], str(d.get("k", "digest24"))
    ref = {"m": match.id, "k": kind}
    if a in ("aa1", "ar1"):
        await _edit_application(session, tenant_id, match.id, int(d["p"]), me.id, removed=a == "ar1")
        await session.commit()
        await channel.answer_button(press, "Добавлен в заявку" if a == "aa1" else "Убран из заявки")
        return await weekly.digest_message(session, tenant_id, match, kind)
    if a == "dg":
        await channel.answer_button(press, "Обновлено")
        return await weekly.digest_message(session, tenant_id, match, kind)

    app = await digests.application(session, tenant_id, match)
    if a == "ar":
        options = [(row.name, row.player_id) for row in app]
        title = "Кого убрать из заявки на игру?"
    else:  # "aa": заявленные на сезон в этой лиге, которых в заявке на игру нет
        _, _, _, entry = await polls._context(session, match, tenant_id)
        in_app = {row.player_id for row in app}
        options = []
        for p in (await session.scalars(select(Player).filter_by(tenant_id=tenant_id, active=True))).all():
            if (
                p.id not in in_app
                and await roster.roster_status(session, tenant_id, p.id, entry.id) == "in_roster"
            ):
                options.append((await polls.player_name(session, tenant_id, p, entry), p.id))
        options.sort(key=lambda t: (t[0].lower().replace("ё", "е"), t[1]))
        title = "Кого добавить в заявку на игру? (только заявленные на сезон)"
    await channel.answer_button(press, "")
    back = Button("← К сводке", {"a": "dg", **ref}, "secondary")
    if not options:
        return ("В заявке на игру никого нет." if a == "ar" else "Добавить некого."), [[back]]
    page = int(d.get("pg", 0))
    chunk = options[page * APP_PAGE : (page + 1) * APP_PAGE]
    rows = [[Button(_label(name), {"a": a + "1", **ref, "p": pid})] for name, pid in chunk]
    last = [back]
    if (page + 1) * APP_PAGE < len(options):
        last.append(Button("Далее →", {"a": a, **ref, "pg": page + 1}))
    rows.append(last)
    return title, rows


async def _edit_application(
    session: AsyncSession, tenant_id: int, match_id: int, player_id: int, by_id: int, *, removed: bool
) -> None:
    row = (
        await session.execute(
            select(ApplicationEntry).filter_by(tenant_id=tenant_id, match_id=match_id, player_id=player_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = ApplicationEntry(tenant_id=tenant_id, match_id=match_id, player_id=player_id, source="captain")
        session.add(row)
    row.source, row.removed_by_captain, row.changed_by_player_id = "captain", removed, by_id


async def on_button(
    session: AsyncSession, channel: ChannelAdapter, tenant_id: int, me: Player, press: ButtonPress
):
    """Возвращает (text, buttons) для замены карточки или None."""
    a, d = press.payload.get("a"), press.payload
    if not await leaders.is_leader(session, tenant_id, me.id):
        await channel.answer_button(press, "Это доступно только руководителям команды")
        return None

    if a == "lineup":  # «Обновить» под старым «составом» (до объединения с «заявкой») — шлём новые карточки
        await channel.answer_button(press, "")
        for text, buttons in await lineup(session, tenant_id):
            await channel.send(press.peer_id, text, buttons)
        return None
    if a == "ac":  # «📋 Текст» — свежая заявка на игру отдельным сообщением
        match = await session.get(Match, d.get("m"))
        try:
            if match is None:
                raise polls.PollError("нет матча")
            await polls._context(session, match, tenant_id)  # чужой матч → PollError
            text = await digests.application_text(session, tenant_id, match)
        except polls.PollError:
            await channel.answer_button(press, "Матч не найден")
            return None
        if not text:
            await channel.answer_button(press, "В заявке на игру пока никого нет")
            return None
        await channel.answer_button(press, "")
        await channel.send(press.peer_id, text)
        return None
    if a in ("dg", "aa", "aa1", "ar", "ar1"):  # сводка за 24 ч / 2 ч и правка заявки на игру
        match = await session.get(Match, d.get("m"))
        try:
            if match is None:
                raise polls.PollError("нет матча")
            return await _application_button(session, channel, tenant_id, me, press, match, d)
        except polls.PollError:  # чужой (другой команды сервиса) или удалённый матч
            await channel.answer_button(press, "Матч не найден")
            return None
    if a == "ap":  # «Заявка: лига» из старых сообщений — карточка матча новым сообщением
        match = await session.get(Match, d.get("m"))
        try:
            if match is None:
                raise polls.PollError("нет матча")
            messages = await weekly.application_card(session, tenant_id, match)
        except polls.PollError:
            await channel.answer_button(press, "Матч не найден")
            return None
        await channel.answer_button(press, "")
        for text, buttons in messages:
            await channel.send(press.peer_id, text, buttons)
        return None
    if a == "tm":  # выбор матча для «время …», когда в этот день несколько
        match = await session.get(Match, d.get("m"))
        try:
            if match is None:
                raise polls.PollError("нет матча")
            day_month = tuple(int(x) for x in str(d["d"]).split(".")) if d.get("d") else None
            text = await set_manual_time(session, tenant_id, match, str(d["t"]), day_month)
        except polls.PollError:
            await channel.answer_button(press, "Матч не найден")
            return None
        await channel.answer_button(press, "Готово")
        return text, []
    if a == "num":  # выбор игрока для «номер …», когда нашлось несколько
        if await roster.roster_status(session, tenant_id, int(d["p"]), int(d["e"])) != "in_roster":
            await channel.answer_button(press, "Игрок не заявлен на сезон в этой лиге")
            return None
        await set_number(session, tenant_id, int(d["p"]), int(d["e"]), str(d["n"]))
        await channel.answer_button(press, f"Номер записан: №{d['n']}")
        return f"Номер записан: №{d['n']}. В заявку на игру пойдёт он.", []
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
    if a in ("gm", "g1", "g2", "g3", "gr", "gx") and not await leaders.is_captain_or_vice(session, me.id):
        await channel.answer_button(press, "Передавать права может только капитан или заместитель")
        return None
    if a == "gm":
        await channel.answer_button(press, "")
        return await rights_menu(session, tenant_id, d.get("pg", 0))
    if a == "gx":  # «Отмена» на любом шаге: ничего не меняем, закрываем диалог
        await channel.answer_button(press, "Отменено")
        return "Передача прав отменена, ничего не изменилось.", [[Button("Открыть снова", {"a": "gm"})]]
    if a == "g1":
        who = await _name(session, tenant_id, d["p"])
        await channel.answer_button(press, "Выберите срок")
        rows = [
            [
                Button(label, {"a": "g2", "p": d["p"], "d": days})
                for days, label in list(DURATIONS.items())[i : i + 2]
            ]
            for i in (0, 2)
        ]
        return f"Передать права капитана: {who}. На какой срок?", [*rows, [RIGHTS_CANCEL]]
    if a == "g2":
        await channel.answer_button(press, "Ещё вопрос")
        rows = [
            [Button("Да, не присылать", {"a": "g3", "p": d["p"], "d": d["d"], "s": 1})],
            [Button("Нет, присылать", {"a": "g3", "p": d["p"], "d": d["d"], "s": 0})],
            [RIGHTS_CANCEL],
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
        who = await _name(session, tenant_id, d["p"])
        until_text = f"до {until:%d.%m}" if until else "до отмены"
        await notify.dm(
            session,
            channel,
            tenant_id,
            d["p"],
            f"Вам переданы права капитана {until_text}: сводки, заявка на игру, сопоставление.",
        )
        await _announce_in_chat(
            session,
            channel,
            tenant_id,
            f"{until_text.capitalize()} обязанности капитана исполняет {who}.",
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
