"""Список «игроки» картинкой PNG: ровные колонки при любом шрифте телефона.

Текстом колонки в VK не выровнять: шрифт пропорциональный и у каждого свой размер. Картинка
рисуется своим шрифтом. Нужен системный шрифт с кириллицей (на VPS — пакет fonts-dejavu-core);
если его нет или Pillow недоступен, `render_players` возвращает None и бот шлёт текст.
"""

import io
import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path

log = logging.getLogger(__name__)

# (обычный, жирный): Linux (DejaVu, Liberation), Windows (Segoe UI, Arial)
FONTS = [
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]

# статус связи с заявкой на сезон → (фон плашки, цвет текста, подпись в легенде)
STYLE = {
    "in_roster": ("#d7f5dd", "#17672b", "№ в заявке"),
    "not_in_roster": ("#ffe3b3", "#8a4b00", "дозаявить"),
    "not_in_league": ("#eceff1", "#90a4ae", "не играет"),
    "unmatched": ("#e3ecff", "#2f54c9", "не сопоставлен"),
}
CELL_TEXT = {"not_in_roster": "нет", "not_in_league": "—", "unmatched": "?"}
NO_DM = "#e53935"  # точка: игрок не открыл личку с ботом

S = 2  # рисуем вдвое крупнее: VK уменьшит, а текст останется чётким
W, PAD, ROW, COL = 540 * S, 16 * S, 34 * S, 74 * S


@dataclass
class TableRow:
    name: str
    cells: list[tuple[str, str]]  # по лигам: (статус, номер)
    dm_open: bool


@dataclass
class PlayersTable:
    leagues: list[str]
    matched: list[TableRow]  # сопоставленные: ФИО из заявки на сезон, по фамилии
    unmatched: list[TableRow] = field(default_factory=list)  # имя из VK
    at: datetime | None = None  # «на ДД.ММ ЧЧ:ММ» в часовом поясе лиги


@cache
def _font_paths() -> tuple[str, str] | None:
    for regular, bold in FONTS:
        if Path(regular).exists() and Path(bold).exists():
            return regular, bold
    log.warning("нет шрифта с кириллицей (%s) — «игроки» будут текстом", ", ".join(r for r, _ in FONTS))
    return None


def render_players(table: PlayersTable) -> bytes | None:
    paths = _font_paths()
    if paths is None:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont  # импорт по требованию: память только когда нужно
    except ImportError:
        log.warning("Pillow не установлен — «игроки» будут текстом")
        return None

    regular, bold = paths
    f = ImageFont.truetype(regular, 15 * S)
    fb = ImageFont.truetype(bold, 15 * S)
    fh = ImageFont.truetype(bold, 19 * S)
    fs = ImageFont.truetype(regular, 12 * S)

    rows = len(table.matched) + len(table.unmatched) + (1 if table.unmatched else 0)
    img = Image.new("RGB", (W, PAD * 2 + 40 * S + ROW * (rows + 1) + 72 * S), "white")
    d = ImageDraw.Draw(img)
    name_w = W - PAD * 2 - COL * len(table.leagues) - 24 * S
    x_cols = [PAD + name_w + i * COL for i in range(len(table.leagues))]
    dot_x = W - PAD - 10 * S

    y = PAD
    d.text((PAD, y), f"Игроки команды: {len(table.matched) + len(table.unmatched)}", font=fh, fill="#222")
    if table.at:
        d.text((W - PAD, y + 4 * S), f"на {table.at:%d.%m %H:%M}", font=fs, fill="#888", anchor="ra")
    y += 40 * S

    d.rectangle((0, y, W, y + ROW), fill="#37474f")
    d.text((PAD, y + ROW // 2), "Игрок", font=fb, fill="white", anchor="lm")
    for x, league in zip(x_cols, table.leagues, strict=True):
        d.text((x + COL // 2, y + ROW // 2), league, font=fb, fill="white", anchor="mm")
    d.text((dot_x, y + ROW // 2), "ЛС", font=fs, fill="white", anchor="mm")
    y += ROW

    def fit(text: str) -> str:  # длинное имя — многоточием, а не переносом
        limit = name_w - 8 * S
        if d.textlength(text, font=f) <= limit:
            return text
        while text and d.textlength(text + "…", font=f) > limit:
            text = text[:-1]
        return text + "…"

    def draw_row(i: int, row: TableRow) -> None:
        nonlocal y
        if i % 2:
            d.rectangle((0, y, W, y + ROW), fill="#f6f7f9")
        d.text((PAD, y + ROW // 2), fit(row.name), font=f, fill="#222", anchor="lm")
        for x, (status, number) in zip(x_cols, row.cells, strict=True):
            bg, fg, _ = STYLE[status]
            text = (number or "б/н") if status == "in_roster" else CELL_TEXT[status]
            d.rounded_rectangle((x + 6 * S, y + 5 * S, x + COL - 6 * S, y + ROW - 5 * S), 6 * S, fill=bg)
            d.text((x + COL // 2, y + ROW // 2), text, font=fb, fill=fg, anchor="mm")
        if not row.dm_open:
            d.ellipse((dot_x - 5 * S, y + ROW // 2 - 5 * S, dot_x + 5 * S, y + ROW // 2 + 5 * S), fill=NO_DM)
        y += ROW

    for i, row in enumerate(table.matched):
        draw_row(i, row)
    if table.unmatched:
        d.text(
            (PAD, y + ROW // 2),
            "Не сопоставлены ни в одной лиге (имя из VK)",
            font=fs,
            fill="#888",
            anchor="lm",
        )
        y += ROW
        for i, row in enumerate(table.unmatched):
            draw_row(i, row)

    y += 12 * S
    x = PAD
    for status, sample in (("in_roster", "12"), *((s, CELL_TEXT[s]) for s in CELL_TEXT)):
        bg, fg, label = STYLE[status]
        d.rounded_rectangle((x, y, x + 34 * S, y + 22 * S), 5 * S, fill=bg)
        d.text((x + 17 * S, y + 11 * S), sample, font=fs, fill=fg, anchor="mm")
        d.text((x + 40 * S, y + 11 * S), label, font=fs, fill="#555", anchor="lm")
        x += 40 * S + int(d.textlength(label, font=fs)) + 14 * S
    y += 30 * S
    d.ellipse((PAD + 12 * S, y + 6 * S, PAD + 22 * S, y + 16 * S), fill=NO_DM)
    d.text((PAD + 40 * S, y + 11 * S), "не открыл личку с ботом", font=fs, fill="#555", anchor="lm")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
