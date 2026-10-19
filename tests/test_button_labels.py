"""Подписи кнопок на телефоне: в ряду из 2+ кнопок — не длиннее PAIR_LABEL_MAX (решение владельца 2026-10-20).

Общая проверка всех клавиатур — в tests/fakes.py (assert_pair_labels при send/edit); здесь — прямые тесты
на клавиатуры, изменённые 20.10 (вымышленные имена).
"""

from basket_assist.bot import leader
from basket_assist.bot.handlers import CHAT_BUTTONS, _help_dm
from basket_assist.channels.base import PAIR_LABEL_MAX, Button
from basket_assist.services import leaders, roster, weekly
from tests.fakes import assert_pair_labels
from tests.test_application import _abl
from tests.test_leader_bot import PETROV, SIDOROV, _big_roster, _ready
from tests.test_roster import CAPTAIN, MEMBERS, _player, _setup


def _pairs_fit(rows: list[list[Button]]) -> None:
    assert rows
    assert_pair_labels(rows)
    for row in rows:
        if len(row) > 1:
            assert all(len(b.label) <= PAIR_LABEL_MAX for b in row), [b.label for b in row]


def test_assert_pair_labels_rejects_long_label_in_pair():
    long = Button("Состав и заявка", {})
    assert_pair_labels([[long]])  # одна в ряду — во всю ширину, можно
    try:
        assert_pair_labels([[long, Button("Игры", {})]])
    except AssertionError as e:
        assert "«Состав и заявка»" in str(e) and "15 символов" in str(e)
    else:
        raise AssertionError("длинная подпись в паре должна ловиться")


def test_pair_label_max_and_copy_button():
    assert PAIR_LABEL_MAX == 12
    assert len(weekly.COPY_BUTTON) <= PAIR_LABEL_MAX


async def test_digest_message_pairs_fit(session, tenant_cfg):
    tenant, _, m = await _abl(session, tenant_cfg)
    for kind in ("digest24", "digest2", "app"):
        _, rows = await weekly.digest_message(session, tenant.id, m, kind)
        _pairs_fit(rows)
        assert [len(row) for row in rows] == [2, 2]
        assert {b.payload["a"] for row in rows for b in row} == {"aa", "ar", "dg", "ac"}


async def test_help_dm_pairs_fit_player_captain_acting(session, tenant_cfg):
    tenant, _ = await _ready(session, tenant_cfg)
    sidorov = await _player(session, tenant, SIDOROV)
    captain = await _player(session, tenant, CAPTAIN)
    petrov = await _player(session, tenant, PETROV)
    await leaders.grant_acting(session, tenant.id, captain.id, petrov.id, None)

    _, player_rows = await _help_dm(session, sidorov.id, False)
    _, captain_rows = await _help_dm(session, captain.id, True)
    _, acting_rows = await _help_dm(session, petrov.id, True)
    for rows in (player_rows, captain_rows, acting_rows):
        _pairs_fit(rows)
    assert [[b.label for b in r] for r in player_rows] == [["Игры", "Мои данные"]]
    assert [[b.label for b in r] for r in captain_rows] == [
        ["Состав и заявка"],
        ["Игры", "Мои данные"],
        ["Игроки", "Права"],
        ["Сопоставление"],
    ]
    assert [[b.label for b in r] for r in acting_rows] == [
        ["Состав и заявка"],
        ["Игры", "Мои данные"],
        ["Игроки"],
        ["Сопоставление"],
    ]


def test_chat_buttons_pairs_fit():
    _pairs_fit(CHAT_BUTTONS)
    assert [[b.label for b in r] for r in CHAT_BUTTONS] == [["Игры"], ["Перейти к боту"]]


async def test_matching_card_pairs_fit_and_has_legend(session, tenant_cfg):
    tenant, entries = await _setup(session, tenant_cfg)
    await _big_roster(session, entries["abl"], 4)
    await roster.import_chat_members(session, tenant.id, MEMBERS, [], None)
    items = await roster.queue(session, tenant.id)
    assert items
    for item in items:
        text, rows = await leader._card(session, tenant.id, item)
        _pairs_fit(rows)
        assert f"\n\n{leader.CARD_LEGEND}" in text
        labels = {b.payload["a"]: b.label for row in rows for b in row}
        assert labels["lnl"] == "Не в лиге" and labels["lx"] == "Не игрок"
        for short in ("«Нет в заявке»", "«Не в лиге»", "«Не игрок»"):
            assert short in leader.CARD_LEGEND
