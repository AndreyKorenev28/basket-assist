# Архитектура MVP

_Статус: принято ([ADR 0005](decisions/0005-mvp-stack-and-architecture.md)). Термины — из `domain.md`._

## 1. Обзор
Один асинхронный процесс Python на VPS (FirstVDS, 1 ядро / 1 ГБ), база — один файл SQLite.

```
                 ┌──────────────────────── basket-assist (1 процесс, asyncio) ────────────────────────┐
  VK Long Poll ──▶ channels/vk ──▶ handlers ──▶ services ──▶ storage (SQLite)                          │
  (беседа, личка)  (ChannelAdapter)  (команды,     (опрос,        ▲                                       │
       ▲                              кнопки,       сводки,       │                                       │
       │                              /start)       заявка, ...)  │                                       │
       └──────── отправка/редактирование ◀──────────┘             │                                       │
                                                                   │                                       │
  API лиг ◀── sources/mtgame, sources/infobasket ◀── sync ────────┘                                       │
  (JSON)        (PlatformAdapter)                                                                          │
                                         scheduler: тик раз в минуту → «что пора сделать?» → services      │
                 └─────────────────────────────────────────────────────────────────────────────────────┘
```

Принципы:
- **Бизнес-логика не знает про VK и про сайты лиг.** VK — за `ChannelAdapter`, лиги — за `PlatformAdapter`.
- **Состояние — в БД, планировщик ничего не помнит.** Раз в минуту тик смотрит в БД и решает, что пора сделать; каждое действие отмечается в журнале (`notification_log`), поэтому повтор безопасен, а перезапуск процесса ничего не ломает. Переносы матчей автоматически сдвигают сводки и напоминания — их не надо «перепланировать».
- **Изоляция по `tenant_id`** во всех таблицах с пользовательскими данными.
- **Минимум ПДн:** храним ФИО, номер, позицию, ID VK; дату рождения — только если включено в настройках лиги (по умолчанию выключено); переписку беседы не храним.

## 2. Структура кода
```
basket-assist/
├── pyproject.toml            # зависимости, настройки ruff/pytest
├── .env.example              # VK_GROUP_TOKEN, VK_GROUP_ID, DB_PATH, ...
├── config/
│   └── tenant.example.toml   # команда, выступления, ссылки лиг, капитан (реальный tenant.toml — вне git)
├── src/basket_assist/
│   ├── main.py               # запуск: БД → адаптеры → long poll + scheduler
│   ├── config.py             # pydantic-settings: .env + tenant.toml
│   ├── domain/               # сущности, enum'ы статусов, чистые правила (без I/O)
│   │   ├── models.py
│   │   └── rules.py          # когда запускать опрос, дедлайны, лимиты 5–12, сопоставление ФИО
│   ├── storage/              # SQLAlchemy 2.0: таблицы, репозитории, миграции Alembic
│   ├── sources/              # PlatformAdapter
│   │   ├── base.py           # интерфейс + DTO (MatchDTO, RosterEntryDTO, StandingDTO)
│   │   ├── mtgame.py         # АБЛ
│   │   └── infobasket.py     # МЛБЛ
│   ├── channels/             # ChannelAdapter
│   │   ├── base.py           # интерфейс: send, edit, pin, answer_button, members, user_names; входящие события
│   │   └── vk.py             # тонкий клиент VK API на httpx + Bots Long Poll
│   ├── services/
│   │   ├── sync.py           # синхронизация расписания, таблиц, заявок на сезон; обнаружение изменений
│   │   ├── roster.py         # привязка игроков к заявкам на сезон, статусы in_roster/not_in_roster/unmatched
│   │   ├── polls.py          # анонс-голосование: публикация, нажатия, обновление
│   │   ├── application.py    # заявка на игру: черновик, правки капитана, номера, лимиты
│   │   ├── digests.py        # сводки капитану 24 ч / 2 ч, мгновенные изменения
│   │   ├── reminders.py      # напоминания молчащим (30 ч), «время не назначено»
│   │   └── opponent.py       # справка по сопернику
│   ├── bot/                  # обработчики входящих: команды, кнопки, «Начать», меню капитана; тексты сообщений
│   └── scheduler.py          # минутный тик
├── tests/                    # pytest; tests/fixtures — образцы ответов API лиг и VK (без ПДн)
└── deploy/                   # systemd unit, скрипт бэкапа, инструкция развёртывания
```

## 3. Модель данных (SQLite)
Общие справочники (без `tenant_id`) — данные лиг, одинаковые для всех команд сервиса:

| Таблица | Ключевые поля |
|---|---|
| `league` | id, name, platform (`mtgame`/`infobasket`), base_url, timezone, min_players, max_players, poll_weekday, poll_time, store_birth_date (bool) |
| `tournament` | id, league_id, external_id, name, season |
| `league_team` | id, league_id, external_id, name |
| `match` | id, tournament_id, external_id, home_team_id, away_team_id, match_date, start_at (UTC, null), time_status, status, venue_name, venue_address, score_home, score_away, source_hash, updated_at |
| `season_roster_entry` | id, league_team_id, tournament_id, external_person_id, last_name, first_name, middle_name, number (null), position, birth_date (null), active |
| `standing` | tournament_id, league_team_id, place, wins, losses, points_for, points_against, updated_at |

Данные команды сервиса (все с `tenant_id`):

| Таблица | Ключевые поля |
|---|---|
| `tenant` | id, name |
| `team_entry` | id, tenant_id, league_team_id, tournament_id, source_url |
| `chat` | id, tenant_id, channel (`vk`), peer_id, is_admin |
| `player` | id, tenant_id, display_name, role (`player`/`captain`/`vice_captain`), active |
| `messenger_account` | id, tenant_id, player_id, channel, external_user_id, dm_allowed, connected_at |
| `roster_link` | id, tenant_id, player_id, season_roster_entry_id, status (`suggested`/`confirmed`/`rejected`/`not_in_roster`/`not_in_league`), confirmed_by, confirmed_at, number_override |
| `permission_grant` | id, tenant_id, player_id, permission (`acting_captain`), suppress_digests_for_grantor (bool), granted_by, granted_at, valid_until (null = до отмены), revoked_at |
| `attendance` | id, tenant_id, match_id, player_id, vote (`going`/`not_going`), updated_at |
| `application_entry` | id, tenant_id, match_id, player_id, source (`poll`/`added_by_captain`), removed_by_captain, updated_at |
| `poll_message` | id, tenant_id, match_id, chat_id, message_ref (cmid), posted_at, last_edited_at, superseded_by |
| `notification_log` | id, tenant_id, kind (`poll_started`, `reminder_30h`, `digest_24h`, `digest_2h`, `no_time_warning`, `not_in_roster`, ...), match_id, player_id, sent_at — **уникальный ключ = идемпотентность** |
| `change_event` | id, match_id / team_entry_id, kind (`rescheduled`, `time_changed`, `roster_added`, `roster_removed`), payload, detected_at, notified |

## 4. Адаптеры
```python
class PlatformAdapter(Protocol):          # sources/base.py
    platform: str                          # "mtgame" | "infobasket"
    def matches_url(self, url: str) -> bool
    async def resolve_team(self, url: str) -> TeamRef               # из ссылки — ID команды/турнира
    async def fetch_matches(self, team: TeamRef) -> list[MatchDTO]
    async def fetch_roster(self, team: TeamRef) -> list[RosterEntryDTO]
    async def fetch_standings(self, team: TeamRef) -> list[StandingDTO]
    async def fetch_game_numbers(self, team: TeamRef) -> dict[str, str]  # номера из протоколов (МЛБЛ)
    roster_fields: set[str]                # какие доп. поля умеет: {"number","position","birth_date"}

class ChannelAdapter(Protocol):           # channels/base.py
    async def events(self) -> AsyncIterator[IncomingEvent]          # сообщение / нажатие / «Начать» / разрешение
    async def send(self, chat: ChatRef, text: str, buttons: Buttons | None) -> MessageRef
    async def edit(self, msg: MessageRef, text: str, buttons: Buttons | None) -> bool  # False → «слишком старое»
    async def pin(self, msg: MessageRef) -> bool
    async def answer_button(self, event: ButtonEvent, toast: str) -> None
    async def chat_members(self, chat: ChatRef) -> list[UserRef]
    async def user_names(self, ids: list[str]) -> dict[str, str]
    def connect_link(self, payload: str) -> str                     # vk.me/club<ID>?ref=<payload>
```
Каждый адаптер покрыт контрактными тестами на сохранённых ответах (`tests/fixtures/`, из `notes/sources/` и `spikes/vk-probe/`).

## 5. Недельный цикл (что делает планировщик)
| Когда | Действие | Сервис |
|---|---|---|
| Раз в сутки (с 04:00 по времени лиги; после простоя — при запуске) | Синхронизация календаря, таблицы, заявки на сезон по всем выступлениям | `scheduler` → `sync` |
| Через 3 ч после начала нашего матча (решение владельца 2026-10-18) | Синхронизация календаря и таблицы этой лиги — счёт в тот же вечер; не успел — подхватит ночная | `scheduler` → `sync` |
| Запуск бота | Перерисовать действующие анонсы (после обновления бота меняется их вид) | `scheduler` → `polls` |
| Ср–Пт, каждый час 9:00–23:00 (решение владельца 2026-10-18: с утра среды; в выходные переносов не бывает) | Синхронизация календаря (появление тура АБЛ, переносы МЛБЛ); после неё — обновление анонсов изменившихся матчей | `scheduler` → `sync` → `polls` |
| Ср 10:00 МСК (настройка лиги) | Анонс-голосование по матчам с известным временем; закрепить; «время не назначено» капитану для остальных | `polls`, `reminders` |
| Время появилось после среды | Анонс сразу | `polls` |
| Ежедневно до появления времени | Повтор «время не назначено» капитану | `reminders` |
| T − 30 ч | Напоминание молчащим (личка, кто не подключён — упоминание в беседе) | `reminders` |
| T − 24 ч | Сводка капитану и заместителю: черновик заявки на игру + кнопки правки | `digests` |
| После T − 24 ч, при каждом изменении | Короткое сообщение капитану об изменении ответа | `digests` |
| T − 2 ч | Финальная сводка, изменения выделены | `digests` |
| Нажатие кнопки | Сохранить отметку, всплывающий ответ, обновить анонс (если `edit` не удался — переопубликовать, ADR 0004) | `polls` |
| Перенос даты / смена времени / изменения в заявке на сезон | Уведомление в беседу; отметки сохраняются, каждому отметившемуся — личка с его ответом и просьбой проверить (решение владельца 2026-10-18) | `sync` → `polls` |
| Новый участник беседы / новая строка заявки на сезон / конфликт | Карточка в очередь сопоставления, уведомление руководителям команды | `roster` |
| Истёк срок передачи прав (и. о. капитана) | Снять права, уведомить и. о., выдавшего и беседу | `roster` |

## 6. Интерфейс в VK
**Беседа команды:** анонс-голосование (закреплён), уведомления о переносах, кнопка «Перейти к боту»: ответ на нажатие открывает личку с ботом (`open_link`), а тому, кто уже открыл личку, бот сразу присылает туда меню с кнопками по его роли.
Обращение к боту в беседе — это упоминание, выбранное из подсказки VK (`[club<ID>|…]`), набранное вручную в начале сообщения «@Basket Assist …» / «@club<ID> …» (имя сообщества бот получает при старте через `groups.getById`) или ответ на сообщение бота. В неподключённой беседе (её нет в конфигурации) бот отвечает на обращение одной фразой «беседа не подключена» и ничего не сохраняет.
**Личка игрока:** «Начать» (разрешение на личные сообщения); напоминания; сигнал «нет в заявке на сезон»; команды «Игры» (ближайшие матчи), «Соперник» (справка), «Мои данные» (привязка, [Это ошибка]).
**Личка руководителя команды** (капитан, заместитель, и. о.): [Сопоставление] → очередь карточек «аккаунт VK → кандидат» с кнопками [Кандидат] [Выбрать из заявки на сезон] [Нет в заявке] [Не играет в лиге] [Не игрок команды] [Пропустить] (очередь по кругу); [Подтвердить все однозначные] для второй лиги. «Игроки» — все игроки со статусами по лигам. «Состав» — состав на ближайший матч каждой лиги по запросу (`digests.lineup_text`: Буду с номерами и ⚠️ для не заявленных, размер заявки на игру, Не буду, Не ответили с 🔕) и кнопка [Обновить]; этот же текст — основа сводок 24 ч / 2 ч. Подсказка «помощь» — с кнопками-командами.
**Права** (капитан, заместитель): [Передать права капитана] → выбрать сопоставленного игрока (ФИО из заявки на сезон, по фамилии) → срок [3 / 7 / 14 дней / до даты / до отмены] → [Не присылать мне сводки на это время]; [Отозвать]; [Отмена] на каждом шаге. Сводки, правка заявки на игру и сопоставление доступны всем руководителям команды.
**Личка капитана и заместителя:** сводки с кнопками:
- [Добавить игрока] → список не ответивших/отказавшихся кнопками;
- [Убрать игрока] → список заявленных;
- номер — команда «номер Петров 23» (по лиге, вместо номера из заявки на сезон);
- время матча вручную — команда «время 25.10 18:30» (пока на сайте лиги времени нет);
- заявка на игру — отдельным сообщением «номер ФИО» построчно (копируется целиком); в финальной сводке изменения с суточной отмечены 🆕.

## 7. Конфигурация MVP
- `.env`: `VK_GROUP_TOKEN`, `VK_GROUP_ID`, `DB_PATH`, `LOG_LEVEL`.
- `config/tenant.toml` (вне git): команда, выступления (ссылки АБЛ Westchester и МЛБЛ МИРЭА), peer_id беседы, VK ID капитана и заместителя. Загружается в БД при старте (идемпотентно). В целевом решении это заменит подключение через бота (сценарий 0).

## 8. Развёртывание и эксплуатация
- VPS FirstVDS, Ubuntu LTS, Python 3.13 в venv, сервис **systemd** (автоперезапуск).
- Бэкап: `sqlite3 .backup` по cron раз в сутки, шифрование (`age`/`gpg`), хранение 14 дней + копия вне сервера.
- Логи: journald, без текста чужих сообщений и без ПДн сверх ID.
- Обновление: `git pull` + миграции Alembic + перезапуск сервиса (скрипт в `deploy/`).

## 9. Этапы MVP
| Этап | Результат | Проверка |
|---|---|---|
| **M0. Каркас** | pyproject, структура, конфиг, БД и миграции, ruff + pytest | `pytest` зелёный, `python -m basket_assist --check` |
| **M1. Данные лиг** | адаптеры `mtgame`, `infobasket`; `sync`; CLI «показать ближайшие матчи и заявки на сезон» | контрактные тесты на образцах + ручной прогон по Westchester и МИРЭА |
| **M2. Бот и опрос** | `channels/vk`, привязка беседы, «Начать», анонс-голосование, обновление, закрепление | тестовая беседа |
| **M3. Заявки на сезон** | привязка игроков, статусы, сигналы «нет в заявке на сезон», номера (МЛБЛ — из протоколов) | тестовая беседа + тесты сопоставления ФИО |
| **M4. Сводки и напоминания** | планировщик, 30 ч / 24 ч / 2 ч, «время не назначено», правки капитана, лимиты 5–12 | тесты правил времени (`rules.py`) + прогон с ускоренным временем |
| **M5. Развёртывание** | VPS, systemd, бэкапы | бот работает на VPS сутки без вмешательства |
| **M6. Пилот** | основная беседа команды, реальный матч | капитан подал заявку по сводке сервиса |

После MVP: справка по сопернику расширенная, скилл проекта, публикация на GitHub, подготовка к защите.
