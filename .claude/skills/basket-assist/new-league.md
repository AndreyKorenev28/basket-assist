# Подключение новой платформы сайта лиги

Лиги заранее не известны: данные читаются через адаптер **платформы** сайта (`PlatformAdapter`), а не через код под
конкретную лигу. Новая лига на уже поддержанной платформе (MTGame, Infobasket) — это только запись `[[leagues]]` в
`config/tenant.toml`; новый адаптер нужен для новой платформы.

## 1. Исследование (до кода)
- Определи платформу сайта лиги: откуда страница берёт расписание, таблицу, заявку на сезон (вкладка Network в браузере,
  открытые JSON API). **Не выдумывать** структуру API — только реальные запросы; непроверенное помечать «⚠ не проверено».
- Запиши выводы в `notes/sources/<лига>/README.md` по образцу `notes/sources/ilovebasket/` и раздел в `docs/data-sources.md`:
  адреса запросов, поля, идентификаторы (команда, турнир, сезон), часовой пояс, ограничения, частота обновления.
- Для разведки допустим одноразовый скрипт в `spikes/` (не код сервиса).
- Выбор источника данных — ADR в `docs/decisions/`.

## 2. Адаптер
- `src/basket_assist/domain/models.py` — новое значение в `Platform`.
- `src/basket_assist/sources/<платформа>.py` — класс с методами протокола `PlatformAdapter` (`sources/base.py`):
  `fetch_matches(team) -> list[MatchDTO]`, `fetch_roster(team) -> list[RosterEntryDTO]`, `fetch_standings(team) -> list[StandingDTO]`,
  атрибуты `platform` и `roster_fields` (какие поля заявки на сезон отдаёт источник: number, position, birth_date).
  Образцы — `mtgame.py`, `infobasket.py`.
- Время матча — с часовым поясом лиги, в базу — UTC. Матч без даты/времени отдавать честно (без выдуманных значений).
- Парсер падает громко (`SourceError`) при неожиданном формате — не глотать ошибки.
- Дата рождения — только если `store_birth_date` включён в настройках лиги (по умолчанию нет, минимум ПДн).
- Зарегистрировать в `make_adapter` (`sources/__init__.py`).

## 3. Тесты
- Образцы ответов — в `tests/fixtures/<платформа>/`, **обезличенные** (добавить URL в `scripts/capture_fixtures.py`,
  см. [fixtures.md](fixtures.md)).
- Контрактные тесты в `tests/test_sources.py`: разбор матчей (дата, время, зал, соперники, статус), заявки на сезон,
  таблицы; ошибки формата → `SourceError`.
- Полный прогон `python -m pytest -q -n auto`, ruff.

## 4. Подключение
Запись `[[leagues]]` в `config/tenant.toml` (`platform`, `base_url`, `team_url`, `team_external_id`,
`tournament_external_id`, `season_external_id`, `timezone`, при необходимости `min_players`/`max_players`, `poll_weekday`)
→ `python -m basket_assist --check` → `--sync --show` на локальной базе → сверка с сайтом → выкладка
(`deploy.sh --with-secrets`, см. [deploy.md](deploy.md)).
