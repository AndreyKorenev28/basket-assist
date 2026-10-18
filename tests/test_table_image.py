"""«игроки» картинкой: services/table_image.render_players (вымышленные имена)."""

import io
import sys
from datetime import UTC, datetime

import pytest

from basket_assist.services import table_image
from basket_assist.services.table_image import PlayersTable, TableRow, render_players

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CELLS = [("in_roster", "7"), ("not_in_roster", "")]


@pytest.fixture
def need_font():
    if table_image._font_paths() is None:
        pytest.skip("нет шрифта с кириллицей")


def _open(png: bytes):
    from PIL import Image

    assert png.startswith(PNG_SIGNATURE)
    img = Image.open(io.BytesIO(png))
    img.load()
    assert img.format == "PNG"
    return img


def _table(n: int, unmatched: int = 0) -> PlayersTable:
    rows = [TableRow(f"Петров{i} Иван", CELLS, dm_open=i % 2 == 0) for i in range(n)]
    extra = [
        TableRow(f"Гость {i}", [("unmatched", ""), ("not_in_league", "")], False) for i in range(unmatched)
    ]
    return PlayersTable(["АБЛ", "МЛБЛ"], rows, extra, datetime(2026, 10, 18, 12, 30, tzinfo=UTC))


def test_render_valid_png_width_1080(need_font):
    img = _open(render_players(_table(3, unmatched=1)))
    assert img.width == 1080
    assert img.mode == "RGB"


def test_render_height_grows_with_rows(need_font):
    heights = [_open(render_players(_table(n))).height for n in (1, 2, 10)]
    assert heights[0] < heights[1] < heights[2]
    one_row = heights[1] - heights[0]
    assert heights[2] - heights[0] == 9 * one_row  # каждая строка — одинаковой высоты
    # блок «не сопоставлены» добавляет строку заголовка и строки игроков
    assert _open(render_players(_table(1, unmatched=2))).height == heights[0] + 3 * one_row


def test_render_all_statuses_and_no_number(need_font):
    row = TableRow(
        "Сидоров Евгений",
        [("in_roster", ""), ("not_in_roster", ""), ("not_in_league", ""), ("unmatched", "")],
        dm_open=True,
    )
    table = PlayersTable(["АБЛ", "МЛБЛ", "ВЛБЛ", "КЛБЛ"], [row], at=None)  # без времени тоже рисуется
    assert _open(render_players(table)).width == 1080


def test_render_long_name_does_not_break(need_font):
    long = "Константинопольский-Иванов-Петров-Сидоров Александр-Максимилиан " * 3
    img = _open(render_players(PlayersTable(["АБЛ", "МЛБЛ"], [TableRow(long, CELLS, False)])))
    assert img.width == 1080
    assert img.height == _open(render_players(_table(1))).height  # без переноса: высота как у короткого имени


@pytest.mark.parametrize("leagues", [[], ["АБЛ", "МЛБЛ"]])
def test_render_empty_table(need_font, leagues):
    img = _open(render_players(PlayersTable(leagues, [])))
    assert img.width == 1080 and img.height > 0


def test_no_font_returns_none(monkeypatch):
    monkeypatch.setattr(table_image, "FONTS", [("/нет/шрифта.ttf", "/нет/шрифта-bold.ttf")])
    table_image._font_paths.cache_clear()
    try:
        assert table_image._font_paths() is None
        assert render_players(_table(2)) is None
    finally:
        table_image._font_paths.cache_clear()


def test_font_search_cached(monkeypatch, tmp_path):
    regular, bold = tmp_path / "r.ttf", tmp_path / "b.ttf"
    regular.write_bytes(b"")
    bold.write_bytes(b"")
    monkeypatch.setattr(table_image, "FONTS", [("/нет/a.ttf", "/нет/b.ttf"), (str(regular), str(bold))])
    table_image._font_paths.cache_clear()
    try:
        assert table_image._font_paths() == (str(regular), str(bold))
        monkeypatch.setattr(table_image, "FONTS", [])
        assert table_image._font_paths() == (str(regular), str(bold))  # из кэша, повторно не ищет
    finally:
        table_image._font_paths.cache_clear()


def test_no_pillow_returns_none(monkeypatch, need_font):
    for name in [m for m in sys.modules if m == "PIL" or m.startswith("PIL.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "PIL", None)  # import PIL → ImportError
    assert render_players(_table(2)) is None
