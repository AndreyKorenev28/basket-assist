"""Скачать ответы API лиг для контрактных тестов и обезличить их.

Запуск: python scripts/capture_fixtures.py
Результат: tests/fixtures/<platform>/*.json. ФИО людей, даты рождения, фото, контакты
заменяются заглушками; названия команд и залов сохраняются.
"""

import json
import re
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

REQUESTS = {
    "mtgame/games_team.json": "https://mtgame.ru/api/v1/tournament/4145/games/?page=1&size=999&team_id=279",
    "mtgame/tournament_teams.json": "https://mtgame.ru/api/v1/tournament/4145/teams/",
    "mtgame/roster.json": "https://mtgame.ru/api/v1/tournament_team/31988/users/",
    "infobasket/team_games.json": "https://reg.infobasket.su/Widget/TeamGames/9056?compId=150000&format=json",
    "infobasket/roster.json": "https://reg.infobasket.su/Widget/TeamRoster/9056?compId=150000&format=json&lang=ru",
    "infobasket/standings.json": "https://reg.infobasket.su/Widget/CompTeamResults/150011?format=json",
}

# Ключи с персональными данными (в любом месте JSON).
PERSON_NAME_KEYS = re.compile(
    r"^(last_name|first_name|middle_name|Person(Last|First|Second|Full)Name(Ru|En))$"
)
DROP_KEYS = re.compile(r"(photo|Photo|avatar|email|phone|Phone|owner|organization|social)", re.I)
BIRTH_KEYS = re.compile(r"^(birth_date|PersonBirth|PersonBirthday|PersonBirthYear|Age)$")


def scrub(obj, counter=[0]):  # noqa: B006 — счётчик на весь прогон
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if DROP_KEYS.search(k):
                out[k] = None
            elif BIRTH_KEYS.match(k) and v not in (None, ""):
                out[k] = {
                    "birth_date": "2000-01-01",
                    "PersonBirth": "01.01.2000",
                    "PersonBirthday": "/Date(946684800000)/",
                    "PersonBirthYear": 2000,
                    "Age": 0,
                }[k]
            elif PERSON_NAME_KEYS.match(k) and isinstance(v, str) and v:
                counter[0] += 1
                out[k] = f"{k.split('_')[0] if '_' in k else k}{counter[0]}"
            else:
                out[k] = scrub(v, counter)
        return out
    if isinstance(obj, list):
        return [scrub(x, counter) for x in obj]
    return obj


def main() -> None:
    with httpx.Client(timeout=30, headers={"User-Agent": "basket-assist/0.1 (fixtures)"}) as client:
        for rel, url in REQUESTS.items():
            r = client.get(url)
            r.raise_for_status()
            data = json.loads(r.content.decode("utf-8-sig"))
            path = OUT / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(scrub(data), ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{rel}: {len(r.content)} байт")


if __name__ == "__main__":
    main()
