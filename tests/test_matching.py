"""Предложение кандидатов для сопоставления (вымышленные имена)."""

from basket_assist.domain.matching import Confidence, RosterName, norm, suggest, translit

ROSTER = [
    RosterName(1, "Петров", "Александр"),
    RosterName(2, "Сидоров", "Евгений"),
    RosterName(3, "Иванов", "Дмитрий"),
    RosterName(4, "Иванов", "Сергей"),
    RosterName(5, "Ёлкин", "Пётр"),
]


def keys(cands):
    return [(c.key, c.confidence) for c in cands]


def test_exact_and_diminutive():
    assert keys(suggest("Саша", "Петров", ROSTER)) == [(1, Confidence.EXACT)]
    assert keys(suggest("Женя", "Сидоров", ROSTER)) == [(2, Confidence.EXACT)]
    assert keys(suggest("Петя", "Елкин", ROSTER)) == [(5, Confidence.EXACT)]  # ё/е


def test_latin_and_swapped():
    assert translit("dmitriy") == "дмитрий"
    assert keys(suggest("Ivanov", "Dmitriy", ROSTER)) == [(3, Confidence.EXACT)]  # переставлены
    assert keys(suggest("Evgeniy", "Sidorov", ROSTER)) == [(2, Confidence.EXACT)]


def test_same_last_name_needs_first_name():
    assert keys(suggest("Серый", "Иванов", ROSTER)) == [(4, Confidence.EXACT)]
    # имя непонятное — оба Иванова как «вероятные»
    assert keys(suggest("Basketball", "Иванов", ROSTER)) == [
        (3, Confidence.PROBABLE),
        (4, Confidence.PROBABLE),
    ]


def test_typo_in_last_name_and_no_match():
    assert keys(suggest("Александр", "Пертов", ROSTER)) == []  # перестановка букв — больше одной правки
    assert keys(suggest("Александр", "Петрова", ROSTER)) == [(1, Confidence.EXACT)]
    assert suggest("Basket", "Ball", ROSTER) == []
    assert norm("DELETED") == "делетед"


def test_compound_last_name_and_unique_first_name():
    roster = ROSTER + [RosterName(6, "Эль Хадиди", "Омар"), RosterName(7, "Фомин", "Фома")]
    assert keys(suggest("Omar", "Hadidi", roster)) == [(6, Confidence.PROBABLE)]
    assert keys(suggest("Foma", "Foma", roster)) == [(7, Confidence.PROBABLE)]  # имя в заявке одно
    assert keys(suggest("Denis", "Petrovich", roster + [RosterName(8, "Орлов", "Денис")])) == [
        (8, Confidence.PROBABLE)
    ]  # отчество вместо фамилии
    assert suggest("Макс", "Прохоров", roster + [RosterName(9, "Карпов", "Максим")]) == []  # фамилия настоящая
    roster2 = roster + [RosterName(10, "Орлов", "Алексей")]
    assert suggest("Alex", "Alex", roster2) == []  # Александр или Алексей — неоднозначно
