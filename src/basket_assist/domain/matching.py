"""Предложение кандидатов «аккаунт мессенджера → строка заявки на сезон».

Бот только ПРЕДЛАГАЕТ — подтверждает руководитель команды (domain.md, «Сопоставление»).
Профиль VK даёт только имя и фамилию, как их записал пользователь: латиница, уменьшительные
имена, переставленные имя/фамилия — всё это учитывается.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

# Уменьшительные и «бытовые» формы → полное имя. Значение — набор, т. к. Alex/Саша/Женя неоднозначны.
DIMINUTIVES: dict[str, set[str]] = {
    "саша": {"александр", "алексей"},
    "шура": {"александр"},
    "алекс": {"александр", "алексей"},
    "леша": {"алексей"},
    "алеша": {"алексей"},
    "макс": {"максим"},
    "степа": {"степан"},
    "влад": {"владислав", "владимир"},
    "вова": {"владимир"},
    "дима": {"дмитрий"},
    "митя": {"дмитрий"},
    "женя": {"евгений"},
    "серый": {"сергей"},
    "сережа": {"сергей"},
    "серега": {"сергей"},
    "виталик": {"виталий"},
    "паша": {"павел"},
    "миша": {"михаил"},
    "коля": {"николай"},
    "ваня": {"иван"},
    "гриша": {"григорий"},
    "петя": {"петр"},
    "костя": {"константин"},
    "толя": {"анатолий"},
    "тема": {"артем"},
    "артемий": {"артем"},
    "игорек": {"игорь"},
    "юра": {"юрий"},
    "слава": {"вячеслав", "владислав", "ярослав"},
    "стас": {"станислав"},
    "андрюха": {"андрей"},
    "андрю": {"андрей"},  # Andrew → андрю после транслитерации
    "эндрю": {"андрей"},
    "денни": {"денис"},
    "дэн": {"денис"},
    "ден": {"денис"},
    "матвей": {"матвей"},
    "миха": {"михаил"},
    "рома": {"роман"},
    "тоха": {"антон"},
    "антоша": {"антон"},
    "егорка": {"егор"},
    "илюха": {"илья"},
    "кирюха": {"кирилл"},
    "кирюша": {"кирилл"},
}

_LAT = [
    ("shch", "щ"),
    ("sch", "щ"),
    ("yo", "е"),
    ("jo", "е"),
    ("zh", "ж"),
    ("kh", "х"),
    ("ts", "ц"),
    ("ch", "ч"),
    ("sh", "ш"),
    ("yu", "ю"),
    ("ju", "ю"),
    ("ya", "я"),
    ("ja", "я"),
    ("ye", "е"),
    ("iy", "ий"),
    ("yy", "ый"),
    ("ey", "ей"),
    ("ay", "ай"),
    ("oy", "ой"),
    ("uy", "уй"),
    ("a", "а"),
    ("b", "б"),
    ("c", "к"),
    ("d", "д"),
    ("e", "е"),
    ("f", "ф"),
    ("g", "г"),
    ("h", "х"),
    ("i", "и"),
    ("j", "й"),
    ("k", "к"),
    ("l", "л"),
    ("m", "м"),
    ("n", "н"),
    ("o", "о"),
    ("p", "п"),
    ("q", "к"),
    ("r", "р"),
    ("s", "с"),
    ("t", "т"),
    ("u", "у"),
    ("v", "в"),
    ("w", "в"),
    ("x", "кс"),
    ("y", "ы"),
    ("z", "з"),
]


def translit(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        for lat, cyr in _LAT:
            if s.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def norm(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[^a-zа-я]", "", s)
    if re.search(r"[a-z]", s):
        s = translit(s)
    return s.replace("й", "и")  # Sergey/Сергей, Dmitriy/Дмитрий — транслитерация «й» неустойчива


_DIMINUTIVES = {norm(k): {norm(v) for v in vs} for k, vs in DIMINUTIVES.items()}


def first_name_forms(first: str) -> set[str]:
    n = norm(first)
    return {n} | _DIMINUTIVES.get(n, set())


def _close(a: str, b: str) -> bool:
    """Расстояние Левенштейна ≤ 1 (для фамилий от 5 букв): опечатки и вариации транслитерации."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            diff += 1
            if diff > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1
            j += 1
    return diff + (len(b) - j) <= 1


class Confidence(StrEnum):
    EXACT = "exact"  # фамилия и имя (с учётом уменьшительных) совпали
    PROBABLE = "probable"  # фамилия совпала (или почти), имя — нет/неоднозначно
    NONE = "none"


@dataclass(frozen=True)
class Candidate:
    key: object  # идентификатор строки заявки на сезон
    confidence: Confidence


@dataclass(frozen=True)
class RosterName:
    key: object
    last_name: str
    first_name: str


def _score(vk_first: str, vk_last: str, r: RosterName) -> Confidence:
    rl, rf = norm(r.last_name), norm(r.first_name)
    best = Confidence.NONE
    for first, last in ((vk_first, vk_last), (vk_last, vk_first)):  # имя и фамилия бывают переставлены
        nl = norm(last)
        if not nl:
            continue
        if nl == rl or _close(nl, rl):
            forms = first_name_forms(first)
            if rf in forms or any(_close(f, rf) for f in forms):
                return Confidence.EXACT
            best = Confidence.PROBABLE
        elif len(nl) >= 4 and nl in rl:  # составная фамилия в заявке: «Эль Хадиди» ↔ «Hadidi»
            best = Confidence.PROBABLE
    return best


def suggest(vk_first: str, vk_last: str, roster: list[RosterName]) -> list[Candidate]:
    """Кандидаты из заявки на сезон, лучшие первыми. Пусто — совпадений нет."""
    scored = [(r, _score(vk_first, vk_last, r)) for r in roster]
    exact = [Candidate(r.key, c) for r, c in scored if c == Confidence.EXACT]
    if exact:
        return exact
    probable = [Candidate(r.key, c) for r, c in scored if c == Confidence.PROBABLE]
    if probable:
        return probable
    # Фамилии в профиле нет («Foma Foma», «Denis Petrovich») — ищем по имени, если оно в заявке одно.
    if not _no_real_last_name(vk_first, vk_last):
        return []
    by_first = [r for r in roster if norm(r.first_name) in first_name_forms(vk_first)]
    return [Candidate(by_first[0].key, Confidence.PROBABLE)] if len(by_first) == 1 else []


def _no_real_last_name(first: str, last: str) -> bool:
    nl = norm(last)
    return (
        nl == norm(first) or nl in first_name_forms(first) or bool(re.search(r"(ович|евич|овна|евна)$", nl))
    )
