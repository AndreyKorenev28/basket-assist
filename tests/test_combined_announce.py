"""Общий анонс игр недели (ADR 0007): несколько матчей в одном закреплённом сообщении, у каждого своя пара
кнопок и свои списки; добавление, голосование, переопубликация, перезапуск, переезд беседы. Вымышленные имена."""

import pytest
from sqlalchemy import select

from basket_assist.bot.handlers import handle
from basket_assist.channels.base import ButtonPress, MessageRef
from basket_assist.services import polls, scheduler, weekly
from basket_assist.storage.tables import Attendance, Chat, PollMessage, utcnow
from tests.fakes import FakeChannel
from tests.test_chat_move import NEW, _move
from tests.test_digests import _match as _rel_match
from tests.test_leader_bot import PETROV, SIDOROV, _assert_vk_keyboard, _ready
from tests.test_roster import CHAT, _player
from tests.test_weekly import _vote

ADDED = "📌 В закреплённый анонс добавлена игра"
HEAD = "📌 Игры недели."


def _labels(rows) -> list[list[str]]:
    return [[b.label for b in row] for row in rows or []]


def _added(ch: FakeChannel) -> list[str]:
    return [t for p, t, _ in ch.sent if p == CHAT and t.startswith(ADDED)]


def _announces(ch: FakeChannel, peer: str = CHAT) -> list[tuple[str, list | None]]:
    return [(t, b) for p, t, b in ch.sent if p == peer and not t.startswith(ADDED)]


def _block(text: str, league: str) -> str:
    """Блок одной игры в общем анонсе (блоки разделены пустой строкой)."""
    found = [b for b in text.split("\n\n") if f"🏀 {league}:" in b]
    assert len(found) == 1, text
    return found[0]


async def _chat(session) -> Chat:
    return (await session.execute(select(Chat).filter_by(peer_id=CHAT))).scalar_one()


async def _two(session, tenant_cfg, ch: FakeChannel, second_days: float = 3):
    """АБЛ через 2 дня и МЛБЛ через second_days; оба опубликованы (второй — в общий анонс, если та же неделя)."""
    tenant, entries = await _ready(session, tenant_cfg)
    m1 = await _rel_match(session, entries["abl"], "a1", days=2)
    m2 = await _rel_match(session, entries["mlbl"], "b1", days=second_days)
    chat = await _chat(session)
    p1 = await polls.publish(session, ch, chat, m1)
    p2 = await polls.publish(session, ch, chat, m2)
    return tenant, entries, m1, m2, p1, p2


# ---------- render_announce / announce_buttons ----------


async def test_render_and_buttons_two_leagues(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m1 = await _rel_match(session, entries["abl"], "a1", days=2)
    m2 = await _rel_match(session, entries["mlbl"], "b1", days=3)
    text = await polls.render_announce(session, tenant.id, [m1, m2])
    assert text.startswith(HEAD)
    assert text.index("🏀 АБЛ:") < text.index("🏀 МЛБЛ:")
    for league in ("АБЛ", "МЛБЛ"):
        b = _block(text, league)
        assert "Буду (0): —" in b and "Не буду (0): —" in b

    rows = await polls.announce_buttons(session, [m1, m2])
    assert _labels(rows) == [["Буду АБЛ", "Не буду АБЛ"], ["Буду МЛБЛ", "Не буду МЛБЛ"]]
    assert [[b.payload["m"] for b in r] for r in rows] == [[m1.id, m1.id], [m2.id, m2.id]]
    assert [b.payload["v"] for b in rows[1]] == ["going", "not_going"]
    _assert_vk_keyboard(rows)


async def test_one_match_render_and_buttons_as_before(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m = await _rel_match(session, entries["abl"], "a1", days=2)
    assert await polls.render_announce(session, tenant.id, [m]) == await polls.render(session, m, tenant.id)
    assert await polls.announce_buttons(session, [m]) == polls.vote_buttons(m.id)


async def test_buttons_same_league_tagged_by_date(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    m1 = await _rel_match(session, entries["abl"], "a1", days=1)
    m2 = await _rel_match(session, entries["abl"], "a2", days=4)
    rows = await polls.announce_buttons(session, [m1, m2])
    d1, d2 = f"{m1.match_date:%d.%m}", f"{m2.match_date:%d.%m}"
    labels = [lab for r in _labels(rows) for lab in r]
    assert labels == [f"Буду {d1}", f"Не буду {d1}", f"Буду {d2}", f"Не буду {d2}"]
    # «Не буду dd.mm» — 13 символов > PAIR_LABEL_MAX: каждая кнопка своим рядом
    assert all(len(r) == 1 for r in rows)
    _assert_vk_keyboard(rows)


async def test_buttons_many_long_tags_numbered_within_vk_limits(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    ms = [await _rel_match(session, entries["abl"], f"a{i}", days=1 + i) for i in range(5)]
    rows = await polls.announce_buttons(session, ms)
    assert _labels(rows)[0] == ["Буду 1", "Не буду 1"]
    assert len(rows) == 5
    _assert_vk_keyboard(rows)
    text = await polls.render_announce(session, tenant.id, ms)
    assert "\n\n1. 🏀 АБЛ:" in text and "\n\n5. 🏀 АБЛ:" in text


async def test_started_match_has_no_buttons(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    started = await _rel_match(session, entries["abl"], "a1", days=-0.01)
    m2 = await _rel_match(session, entries["mlbl"], "b1", days=2)
    rows = await polls.announce_buttons(session, [started, m2])
    assert _labels(rows) == [["Буду МЛБЛ", "Не буду МЛБЛ"]]
    # одиночный анонс — кнопки как раньше; начавшийся матч отклоняет голос в on_vote
    assert len(await polls.announce_buttons(session, [started])) == 1


# ---------- publish: добавление в общий анонс ----------


async def test_second_match_same_week_added_to_announce(session, tenant_cfg):
    ch = FakeChannel()
    _, _, m1, m2, p1, p2 = await _two(session, tenant_cfg, ch)
    assert len(_announces(ch)) == 1, "нового анонса нет"
    assert p2.message_ref == p1.message_ref and p2.match_id == m2.id
    ref = MessageRef(CHAT, p1.message_ref)
    assert ch.pins == [ref], "закрепление не повторяется"
    assert ch.edits[-1][0] == ref
    text = ch.messages[(CHAT, p1.message_ref)]
    assert text.startswith(HEAD) and "🏀 АБЛ:" in text and "🏀 МЛБЛ:" in text
    assert _labels(ch.buttons[(CHAT, p1.message_ref)]) == [
        ["Буду АБЛ", "Не буду АБЛ"],
        ["Буду МЛБЛ", "Не буду МЛБЛ"],
    ]
    added = _added(ch)
    assert len(added) == 1 and "(МЛБЛ)" in added[0] and "«Буду» или «Не буду»" in added[0]

    # повторный publish — тот же опрос, ничего не отправлено
    sent, edits = len(ch.sent), len(ch.edits)
    assert (await polls.publish(session, ch, await _chat(session), m2)).id == p2.id
    assert (len(ch.sent), len(ch.edits)) == (sent, edits)


async def test_match_of_other_week_gets_own_announce(session, tenant_cfg):
    ch = FakeChannel()
    _, _, _, m2, p1, p2 = await _two(session, tenant_cfg, ch, second_days=2 + polls.GROUP_DAYS + 1)
    assert p2.message_ref != p1.message_ref
    assert len(_announces(ch)) == 2 and len(ch.pins) == 2 and _added(ch) == []
    assert ch.pins[-1] == MessageRef(CHAT, p2.message_ref)
    assert _announces(ch)[1][1] == polls.vote_buttons(m2.id)


async def test_announce_with_only_started_matches_not_reused(session, tenant_cfg):
    tenant, entries = await _ready(session, tenant_cfg)
    started = await _rel_match(session, entries["abl"], "a1", days=-0.01)
    m2 = await _rel_match(session, entries["mlbl"], "b1", days=1)
    ch = FakeChannel()
    chat = await _chat(session)
    p1 = await polls.publish(session, ch, chat, started)
    p2 = await polls.publish(session, ch, chat, m2)
    assert p2.message_ref != p1.message_ref
    assert len(_announces(ch)) == 2 and _added(ch) == []


# ---------- голосование в общем анонсе ----------


async def test_vote_on_second_match_counted_for_its_match(session, tenant_cfg):
    ch = FakeChannel()
    _, _, m1, m2, p1, _ = await _two(session, tenant_cfg, ch)
    press = ButtonPress(CHAT, PETROV, MessageRef(CHAT, p1.message_ref), {"m": m2.id, "v": "going"}, "e")
    await handle(session, ch, press)
    rows = (await session.scalars(select(Attendance))).all()
    assert [(a.match_id, a.vote) for a in rows] == [(m2.id, "going")]
    assert ch.toasts[-1] == "Отмечено: Буду"
    assert ch.edits[-1][0] == MessageRef(CHAT, p1.message_ref)
    text = ch.messages[(CHAT, p1.message_ref)]
    assert "Буду (1)" in _block(text, "МЛБЛ") and "Буду (0)" in _block(text, "АБЛ")
    assert len(_announces(ch)) == 1


# ---------- переопубликация ----------


async def test_edit_failed_republishes_whole_announce(session, tenant_cfg):
    ch = FakeChannel()
    tenant, _, m1, m2, p1, p2 = await _two(session, tenant_cfg, ch)
    old_ref = p1.message_ref
    ch.editable = False
    await polls.refresh(session, ch, tenant.id, m1)
    await session.refresh(p1)
    await session.refresh(p2)
    assert p1.superseded_by_id and p2.superseded_by_id
    new = {
        p.match_id: p for p in (await session.scalars(select(PollMessage).filter_by(superseded_by_id=None)))
    }
    assert set(new) == {m1.id, m2.id}
    assert new[m1.id].message_ref == new[m2.id].message_ref != old_ref
    assert (p1.superseded_by_id, p2.superseded_by_id) == (new[m1.id].id, new[m2.id].id)
    text, buttons = _announces(ch)[-1]
    assert "🏀 АБЛ:" in text and "🏀 МЛБЛ:" in text and len(buttons) == 2
    assert ch.pins[-1] == MessageRef(CHAT, new[m1.id].message_ref)
    assert ch.messages[(CHAT, old_ref)] == polls.STALE_TEXT


# ---------- перезапуск опроса одного матча ----------


@pytest.mark.parametrize(
    "which",
    [
        "added",
        "first",
    ],
)
async def test_restart_one_match_of_combined_announce(session, tenant_cfg, which):
    """Перезапуск одной игры общего анонса: другая остаётся с отметками; недельный цикл возвращает игру
    в тот же анонс без повторного «добавлена игра», и кнопки этой игры снова работают.
    which: «added» — игра, дописанная в анонс; «first» — игра, с которой анонс был опубликован."""
    ch = FakeChannel()
    tenant, _, m1, m2, p1, _ = await _two(session, tenant_cfg, ch)
    (m, lg), (other, other_lg) = (
        ((m2, "МЛБЛ"), (m1, "АБЛ")) if which == "added" else ((m1, "АБЛ"), (m2, "МЛБЛ"))
    )
    ref = p1.message_ref
    added_before = len(_added(ch))
    petrov, sidorov = await _player(session, tenant, PETROV), await _player(session, tenant, SIDOROV)
    await _vote(session, tenant.id, m, petrov, "going")
    await _vote(session, tenant.id, other, sidorov, "going")

    assert await polls.restart(session, ch, tenant.id, m) == [(petrov.id, "going")]
    left = (await session.scalars(select(Attendance))).all()
    assert [(a.match_id, a.player_id) for a in left] == [(other.id, sidorov.id)], "отметки другой игры целы"
    assert ch.edits[-1][0] == MessageRef(CHAT, ref)
    text = ch.messages[(CHAT, ref)]
    assert f"🏀 {lg}:" not in text and f"🏀 {other_lg}:" in text and "Буду (1)" in text
    assert ch.buttons[(CHAT, ref)] == polls.vote_buttons(other.id)
    assert await polls._current_poll(session, tenant.id, m.id) is None

    # нажатие на кнопку закрытого опроса — не засчитывается
    press = ButtonPress(CHAT, PETROV, MessageRef(CHAT, ref), {"m": m.id, "v": "going"}, "e1")
    await handle(session, ch, press)
    assert ch.toasts[-1] == polls.CLOSED_TOAST
    assert len((await session.scalars(select(Attendance))).all()) == 1

    # недельный цикл возвращает матч в тот же общий анонс
    await weekly._ensure_poll(session, ch, tenant.id, m, utcnow())
    back = await polls._current_poll(session, tenant.id, m.id)
    assert back is not None and back.message_ref == ref
    assert len(_announces(ch)) == 1 and len(ch.pins) == 1
    text = ch.messages[(CHAT, ref)]
    assert "🏀 АБЛ:" in text and "🏀 МЛБЛ:" in text and len(ch.buttons[(CHAT, ref)]) == 2

    # теперь голос на кнопке этой игры снова засчитывается
    press = ButtonPress(CHAT, PETROV, MessageRef(CHAT, ref), {"m": m.id, "v": "not_going"}, "e2")
    await handle(session, ch, press)
    assert ch.toasts[-1] == "Отмечено: Не буду"
    votes = {(a.match_id, a.vote) for a in (await session.scalars(select(Attendance))).all()}
    assert votes == {(m.id, "not_going"), (other.id, "going")}

    # беседа уже знает о перезапуске («отметьтесь заново в закреплённом анонсе») — «добавлена игра» не нужно
    assert len(_added(ch)) == added_before


async def test_restart_last_match_closes_announce(session, tenant_cfg):
    """Обе игры перезапущены — на сообщении CLOSED_TEXT без кнопок."""
    ch = FakeChannel()
    tenant, _, m1, m2, p1, _ = await _two(session, tenant_cfg, ch)
    await polls.restart(session, ch, tenant.id, m1)
    await polls.restart(session, ch, tenant.id, m2)
    assert ch.messages[(CHAT, p1.message_ref)] == polls.CLOSED_TEXT
    assert ch.buttons[(CHAT, p1.message_ref)] is None


# ---------- переезд беседы и обновление анонсов ----------


async def test_relocate_combined_announce_one_message(session, tenant_cfg):
    ch = FakeChannel()
    tenant, _, m1, m2, p1, p2 = await _two(session, tenant_cfg, ch)
    await _move(session, tenant_cfg)
    ch2 = FakeChannel()
    assert await polls.relocate(session, ch2, tenant.id) == 2
    posts = _announces(ch2, NEW)
    assert len(posts) == 1
    text, buttons = posts[0]
    assert "🏀 АБЛ:" in text and "🏀 МЛБЛ:" in text and len(buttons) == 2
    await session.refresh(p1)
    await session.refresh(p2)
    n1, n2 = (
        await session.get(PollMessage, p1.superseded_by_id),
        await session.get(PollMessage, p2.superseded_by_id),
    )
    assert (n1.match_id, n2.match_id) == (m1.id, m2.id) and n1.message_ref == n2.message_ref
    assert ch2.pins == [MessageRef(NEW, n1.message_ref)]
    assert ch2.messages[(CHAT, p1.message_ref)] == polls.MOVED_TEXT


async def test_refresh_active_polls_edits_combined_once(session, tenant_cfg):
    ch = FakeChannel()
    _, _, _, _, p1, _ = await _two(session, tenant_cfg, ch)
    ch2 = FakeChannel()
    assert await scheduler.refresh_active_polls(session, ch2) == 1
    assert [e[0] for e in ch2.edits] == [MessageRef(CHAT, p1.message_ref)]


async def test_max_matches_in_announce(session, tenant_cfg):
    """Не больше MAX_IN_ANNOUNCE игр в одном сообщении — следующая публикуется отдельно."""
    tenant, entries = await _ready(session, tenant_cfg)
    chat = await _chat(session)
    ch = FakeChannel()
    refs = []
    for i in range(polls.MAX_IN_ANNOUNCE + 1):
        m = await _rel_match(session, entries["abl"], f"a{i}", days=1 + i * 0.1)
        refs.append((await polls.publish(session, ch, chat, m)).message_ref)
    assert len(set(refs[:-1])) == 1 and refs[-1] != refs[0]
    assert len(_announces(ch)) == 2
    for rows in ch.buttons.values():
        _assert_vk_keyboard(rows)
