# Источник: ABL (ablforpeople.com) — разведка для адаптера платформы

Дата исследования: **2026-10-18**. Всё ниже получено реальными запросами (curl) к сайту и API;
непроверенное помечено «⚠ не проверено». Всего ~45 запросов, последовательно.

Пример: **наша команда Westchester** (турнир «Шелепиха», сезон 2026-2027).

---

## TL;DR

- Сайт — **Angular SSR-фронтенд платформы MTGame** (mtgame.ru). Все данные отдаёт **публичный JSON REST API
  `https://mtgame.ru/api/v1/...`** без авторизации. HTML парсить не нужно.
- ID лиги ABL = **2**, текущий сезон = **231** («Сезон 2026-2027»).
- Westchester: `team_id = 279` (базовая команда, имя в базе **«Whestchester»** с опечаткой),
  `tournament_team_id = 31988` (в турнире называется **«Westchester»**), турнир `4145` «Шелепиха».
- **Расписание:** матч появляется в API только когда тур уже сформирован, **сразу с конкретным временем**.
  Матчей «дата есть, время нет» в API нет ни одного; будущих туров тоже нет. Подробнее — раздел 4.
- **Ростер** с ФИО, номером, датой рождения, ростом/весом, амплуа, фото — открыт публично.
- Регламент: заявка на матч (отметка игроков) — **не позднее чем за 1 час до игры**; мин. 4 игрока на площадке,
  в сезонной заявке 5–25 игроков, в протоколе матча до 12 (`max_game_users`).

---

## 1. Платформа / движок

| Что видно | Где |
|---|---|
| `X-Powered-By: Express`, `Server: nginx/1.28.3` | заголовки `https://ablforpeople.com/` |
| Angular (бандлы `main-es2015.*.js`, `runtime-*`, `<script id="serverApp-state">` — Angular TransferState) | HTML главной |
| `environment = {apiUrl:"https://mtgame.ru", cdnHost:"https://cdn.mtgame.ru", centrifugoUrl:"wss://ws2.mtgame.ru/connection/websocket"}` | `main-es2015.11aa7279ea06d56b28ae.js` |
| Ссылка в футере на `https://mtgame.ru`, картинки на `https://fs.mtgame.ru/...` | HTML |

Вывод: **ablforpeople.com — «white-label» сайт лиги на платформе MTGame** (та же платформа обслуживает и другие лиги;
в бандле есть футбол/хоккей/волейбол и т.д.). Лига определяется по домену:
`GET https://mtgame.ru/api/v1/league/by_domain/ablforpeople.com/` → `{"id": 2, "name": "ABL", "alias": "abl", ...}`.

Это значит, что **адаптер можно сделать как «MTGame-адаптер»** с параметром `league_id` — вероятно,
подойдёт и для других лиг на MTGame (⚠ не проверено).

SSR-страница кладёт в `serverApp-state` ответы API (ключи вида `G.https://mtgame.ru/api/v1/...`) —
по ним удобно подсматривать, какие эндпоинты вызывает каждая страница.
В JSON внутри HTML кавычки экранированы как `&q;` (`&a;` = `&`, `&s;` = `'`, `&l;`/`&g;` = `<`/`>`).

## 2. Структура: лига → сезон → турнир (дивизион) → группа → команда

```
league (2, ABL)
 └─ tournament_season (231, "Сезон 2026-2027", status=open)
     └─ tournament (4145 "Шелепиха", alias shelepiha27, status=in_progress, settings.type="swiss")
         ├─ tournament_group (6514 "Шелепиха")
         └─ tournament_team (31988 "Westchester")  ──►  team (279 "Whestchester")  — сквозная сущность через сезоны
               └─ tournament_team_user (заявка игрока)  ──► team_user ──► user / league_player
```

- «Дивизионы» на сайте = **турниры** сезона. Сезон 231 содержит 19 турниров:
  Шелепиха (4145), Перово (4147), Москвич (4148), Профа (4149), WBL (4144), PURE (4152), детские PYC/PPC U10–U16,
  PLUS (4261), ABL PRO Астана (4299), ABL Астана QR (4286), «ABC Almaty» 1/2 дивизион (4314/4315).
  Шелепиха: `description: "Квалификация"`, `date 2026-09-05 … date_end 2026-12-27`, 32 команды.
- **Текущий сезон:** `GET /api/v1/league/2/tournament_seasons/` → массив; брать первый со `status:"open"`
  (они отсортированы от нового к старому; у 231/152/64/48 всех `status:"open"`, `start_date:null`,
  поэтому надёжнее — максимальный `id` или имя). Сайт сам берёт «активный сезон» из этого списка.
- Турниры сезона: `GET /api/v1/tournament_season/231/tournaments/`.
- Команды/таблица турнира: `GET /api/v1/tournament/4145/teams/`.
- История команды по сезонам: `GET /api/v1/public_team/279/tournament_teams/?league_id=2`
  (без `league_id` → `{"error":"league_id filter is required"}`). У Westchester 15 записей с 2019/2020
  (названия менялись: Westchester Knicks → Unkillable members → Westchester/Whestchester).

**Публичные URL сайта (проверены, отдают 200 и SSR):**

| Страница | URL | `<title>` |
|---|---|---|
| Команда | `https://ablforpeople.com/team/279` (по **team_id**, не tournament_team_id) | «Команда Westchester - ABL» |
| Турнир | `https://ablforpeople.com/tournament/4145` | «Шелепиха» |
| Матч | `https://ablforpeople.com/game/163719` | «Статистика. Матч Westchester против Pokrasote - ABL» |
| Календарь | `https://ablforpeople.com/calendar` | «Календарь игр и расписание матчей ABL в Москве» |
| Таблицы | `https://ablforpeople.com/tournaments` | «Турнирная таблица и результаты…» |
| Ещё в меню | `/divisions`, `/statistic`, `/documents`, `/media`, `/contacts` | |

`/team/31988` отдаёт 200, но пустую страницу (title «ABL») — tournament_team_id в URL не годится.
Есть роут `league-player/:id` — точный сегмент ⚠ не проверен.

## 3. Страница команды

`/team/279` при SSR вызывает:
- `GET /api/v1/public_team/279/` → `{"id":279,"name":"Whestchester","logo":{...},"city":{"name":"Москва"},"sport":{...},"unique_code":"3D0M"}`
- `GET /api/v1/public_team/279/tournament_teams/?league_id=2` → участие во всех турнирах лиги (id, name, points, group, division, tournament{id,name,season,status}).

Внутренние вкладки (игры/состав/статистика) грузятся в браузере — ниже эндпоинты для них.

## 4. Календарь / расписание (ключевой вопрос)

**Эндпоинты:**
- Игры турнира: `GET /api/v1/tournament/4145/games/?page=1&size=999[&team_id=279][&tournament_tour=7]`
- Игры всей лиги: `GET /api/v1/league/2/games/?page=1&size=100&date_from=2026-10-18[&date_to=...]`
- Игры сезона (как на /calendar): `GET /api/v1/tournament_season/231/games/?page=1&size=15`
- Даты, в которые есть игры: `GET /api/v1/tournament/4145/games_dates/` → `[{"date":"2026-09-05"},…,{"date":"2026-10-17"}]`
- Площадки лиги: `GET /api/v1/league/2/courts/` (⚠ сам ответ не запрашивал; адрес и так есть в каждой игре).

Фильтры, найденные в бандле (`Wn()`): `tournament_ids`, `tournament_stage_id`, `tournament_round_id`,
`tournament_group_id`, `division_id`, `tournament_tour`, `playoff_stage`, `playoff_round`, `team_id`,
`status`/`statuses`, `tournament_court_id`, `league_player_id`, `user_id`, `query`, `date_from`, `date_to`, `is_playoff`.
Пагинация: `page`, `size`; общее число — в заголовке **`X-Page-Count`** (это число записей, не страниц).

**Фрагмент игры** (полный — `sample_games_team.json`):
```json
{"id":163719,"datetime":"2026-10-17T17:40:00.000000Z","status":"closed","tournament_tour":7,
 "team":{"id":279,"name":"Whestchester"},"tournament_team":{"id":31988,"name":"Westchester"},
 "competitor_team":{"id":2301,"name":"Pokrasote"},"team_score":57,"competitor_team_score":76,
 "score_by_period":{"1":[..,..],...},"result_type":"...",
 "tournament_court":{"id":2,"name":"Шелепиха","address":"Россия, Москва, 2-я Магистральная улица, 16А"},
 "location":"Россия, Москва, 2-я Магистральная улица, 16А"}
```
- `datetime` в UTC (суффикс `Z`); сайт показывает по Москве: `07:00Z` → «18 октября, 10:00» на /calendar.
- `status`: `open` (запланирован), `in_progress`, `closed` (видели только эти три).
- Хозяин — `team` (слева); по регламенту п.10.3 хозяин играет в светлой форме.
- Зал/адрес: `tournament_court.name` + `tournament_court.address`; у турнира ещё `location: "2 магистральная 16с1"`.

**Как выглядит «дата без времени»:**
- Во всех 112 играх Шелепихи и во всех будущих играх лиги (выборка `date_from=2026-10-18`: 100 игр + 21 после 19.10)
  **нет ни одной игры с пустым `datetime` или временем 00:00**. Текста вроде «время уточняется» в бандле нет.
- Шелепиха — **швейцарская система** (`settings.type:"swiss"`): игры создаются **по одному туру**.
  На 18.10 в API есть туры 1–7 (по 16 игр), тура 8 нет вообще; у Westchester все 7 игр `closed`,
  **ближайшего матча и соперника в API пока нет**. ID игр идут блоками по турам (тур 6: 161123–161138,
  тур 7: 163706–163721), что подтверждает поэтапное создание.
- Значит, для ABL «дата известна с начала сезона» берётся **не из API**, а из правила: игры по выходным
  (регламент п.7.2: «в выходные дни… начинаются не ранее 8:00 и заканчиваются не позднее 23:59»);
  тур идёт раз в неделю (даты игр Шелепихи — каждые сб/вс с 05.09).
- Регламент п.5.8: «Публикация договорённостей по желаемому временному интервалу игры осуществляется
  в **капитанском чате турнира** в период с 12:00 понедельника до 18:00 вторника». Т.е. время согласуется
  в начале недели, после чего тур появляется на сайте. Точный момент публикации ⚠ не проверено
  (можно установить опросом API в течение недели 19–24.10).
- Слоты Шелепихи — шаг 70 минут: 10:00, 11:10, 12:20, … 21:40 МСК (по 07:00Z…18:40Z).
- Отдельных новостей «расписание тура» нет: новости лиги — видео («ПРЕГЕЙМ - ШЕЛЕПИХА - 6 ТУР», type `video`,
  ссылка на vkvideo), выходят в пятницу перед туром.

**Рекомендация:** опрашивать `GET /tournament/{id}/games/?team_id={team_id}&date_from={сегодня}`
(или `/team/{id}/games/` ⚠ не проверено) раз в несколько часов со вторника по пятницу; появление игры = время назначено.
До этого в ассистенте держать «ожидаемую дату» по правилу (ближайшие сб/вс) с пометкой «время не назначено».
Изменение `datetime` у существующей игры = перенос.

## 5. Таблица, результаты, протоколы, статистика

**Таблица** = `GET /api/v1/tournament/4145/teams/` (уже отсортирована по месту; сайт берёт ту же сущность).
Поля: `games_count, won_games_count, lose_games_count, won_games_percent, points, score_sum, missed_sum,
plus_minus, buchholz_score` (Бухгольц — для швейцарки), `group`, `last_five_games[]` (со счётом по четвертям).
Пример: KB Magic — 1-е место, 7 игр 6–1, 13 очков; **Westchester — 28-е из 32, 2–5, 9 очков, 463:525, Бухгольц 65.**
Очки = 2 за победу + 1 за поражение (вычислено по данным). Место по регламенту — по % побед (ст.16).
Есть выгрузка `GET /tournament/{id}/table_file/?file_type=...` (⚠ не проверено). Плей-офф: `/tournament/{id}/playoff/`.

**Протокол/boxscore матча** (всё 200 без авторизации), пример матча 163719:
- `GET /api/v1/tournament_basketball_game/163719/` — сама игра;
- `GET /api/v1/tournament_basketball_game/163719/user_statistic/` — 17 строк, по игроку: `points, two/three_points_made/attempts,
  free_throws_*, rebounds (off/def), assists, steals, blocks, turnovers, personal_fouls, game_time (сек), plus_minus, player_efficiency`, ключ `game_user_id`;
- `GET /api/v1/tournament_game/163719/users/` — заявка на матч (17 записей: `number, is_mvp, team_user, tournament_team_user`);
- `GET /api/v1/tournament_basketball_game/163719/team_statistic/` — командная статистика (проценты, TS%, фолы и т.д.);
- `GET /api/v1/tournament_basketball_game/163719/logs/` — play-by-play (вызывается SSR страницы матча; содержимое ⚠ не смотрел);
- PDF-протокол: `.../game_protocol/?file_type=...` (⚠ не проверено).

**Статистика игроков за турнир (лидеры, справка по сопернику):**
`GET /api/v1/basketball_statistic/?group_by=tournament_team_user&tournament_id=4145&tournament_team_id=31988&per_game=1&page=1&size=50`
→ 14 строк по игрокам Westchester: `games_count, points, rebounds, assists, player_efficiency, …, tournament_team_user{league_player,team_user}`.
Без `tournament_team_id` — по всему турниру (можно сортировать на своей стороне → лидеры).
`/basketball_statistic_leaders/` требует `group_by` и `fields` и «один из основных фильтров»; мой запрос
с `tournament_id=4145&fields=points,rebounds,assists` вернул **500** — правильный набор параметров ⚠ не выяснен.

## 6. Заявка на сезон (ростер)

`GET /api/v1/tournament_team/31988/users/` → 19 игроков Westchester. Структура одной записи (`sample_roster_entry.json`, ПДн заменены):

| Поле | Путь | Заполнено у Westchester |
|---|---|---|
| ФИО | `team_user.user.{last_name, first_name, middle_name}` (дублируется в `league_player`) | 19/19 (отчество 2/19) |
| Номер | `team_user.number` (строка) | 19/19 |
| Дата рождения | `team_user.user.birth_date`, `league_player.birth_date` | **19/19, публично** |
| Рост / вес | `team_user.user.profile.{height, weight}` | рост 18/19 |
| Амплуа | `team_user.user.basketball_profile.position` (`center`, `small_forward`, `shooting_guard`, …), `work_hand` | 15/19 |
| Фото | `team_user.user.photo.path` (https://fs.mtgame.ru/...) | 19/19 |
| Роль в команде | `team_user.role` (`member`/`admin`) | |
| Статус | `active`, `disqualified`, `disqualification`, `playoff_admission` | |
| Доп. поля лиги | `league_player.field_values[]`, напр. «Мед. справка»: «сдал»/«не сдал» | |

Регламент (Прил.1, п.2.2) требует в заявке: ФИО, амплуа, дату рождения, фото, игровой номер, e-mail
(e-mail в публичном ответе ростера не виден).

## 7. Регламент

`GET /api/v1/league/2/documents/` → 1 файл: `https://fs.mtgame.ru/Reglament_yYArJ.pdf` («Регламент.pdf», 15 стр., сезон 2026/2027).
Также `tournament.regulation_files` (у Шелепихи пусто). Ключевое для ассистента:
- **Заявка на матч:** «Представитель команды обязан отметить игроков в заявке на матч **не менее чем за один час до игры**» (Прил.1, ст.5.3). Делается в электронном протоколе на сайте (личный кабинет; через API — ⚠ не проверено, требует авторизации).
- **Минимум:** игра не начинается, если на площадке нет **4 игроков**; иначе техпоражение 20:0 (п.5.4–5.5).
- **Сезонная заявка:** от 5 до 25 игроков (Прил.1 п.2.1, п.5.6); `settings.max_team_players: 25`. Дозаявка — весь турнир (Прил.1 п.3.2).
- **Максимум на матч:** в регламенте явно не нашёл; в настройках турнира `max_game_users: 12`, `max_game_players: 5` (на площадке).
- **Легионеры:** в заявке на игру не более 2 «легионеров» (Прил.1 п.3.7).
- **Оплата взноса за игру — за 2 суток до матча** (п.12.5).
- Время матча: согласование в капитанском чате пн 12:00 – вт 18:00 (п.5.8); игры в выходные 8:00–23:59 (п.7.2).
- Плей-офф: допуск при 30% сыгранных матчей за команду (Прил.1 п.3.6).
- Формат: 4×12 мин «грязного» времени, FIBA 2026.

## 8. API / экспорт / ограничения

- **JSON REST** `https://mtgame.ru/api/v1/` — GET без авторизации, `Allow: GET, HEAD, OPTIONS`,
  `Access-Control-Allow-Credentials: true`. Ошибки — JSON (`{"error": ...}`, DRF-стиль `{"field":["This field is required."]}`).
- Авторизация (JWT: `/api/v1/auth/jwt_login/`, VK/Facebook) нужна только для действий (заявка, инвайты, опросы команды).
  У платформы есть «командные» функции: `team/{id}/events/`, `team/{id}/polls/`, `team_event_invite/{id}/accept|decline` — потенциально сбор состава (⚠ не проверено, требует логина).
- Realtime: Centrifugo WebSocket `wss://ws2.mtgame.ru/connection/websocket` (⚠ не проверено).
- Лента событий турнира: `GET /api/v1/tournament/4145/events/?page=1&size=12` → `event: game_closed | media_added`, с `datetime`. Полезно для инкрементального обновления результатов.
- **RSS/iCal/экспорт календаря — не найдено** (в бандле нет `.ics`/`ical`/`rss`). Есть только файлы таблицы/протоколов.
- `robots.txt` на обоих доменах: только `User-agent: *` (без Disallow).
- Cloudflare/капча/rate-limit-заголовков **не обнаружено** (nginx, `X-Cache-Status` на сайте). Лимиты ⚠ не проверены — ходить бережно, кэшировать.
- Пагинация: `X-Page-Count` = общее число записей; `X-Page-Next-Link` (у новостей заполнен).

## 9. Соцсети

Из `league.social_links`: **VK** `https://vk.com/ablrussia`, **Telegram** `https://t.me/ablmsc` (канал «ABL FOR PEOPLE»),
Instagram `https://www.instagram.com/abl_moscow/`; в `links` ещё `https://t.me/ablforpeople.com`.
Видео — VK Video (`vkvideo.ru`, группы `-154666590`, `-199224607`).
Публичное превью `https://t.me/s/ablmsc` (последние 15 постов): прегеймы, хайлайты, MVP — **объявлений о времени матчей нет**.
VK-группу без авторизации прочитать не удалось (отдаётся заглушка «VK | VK»).
Оперативное время, судя по регламенту, согласуется в **закрытом капитанском чате** (где — ⚠ не выяснено).

---

## Цепочка для Westchester (2026-10-18)

1. Лига: `league/by_domain/ablforpeople.com/` → id 2.
2. Сезон: `league/2/tournament_seasons/` → 231.
3. Команда: `public_team/279/tournament_teams/?league_id=2` → в сезоне 231 только `tournament_team 31988`, турнир 4145 «Шелепиха».
4. Расписание: `tournament/4145/games/?team_id=279` → 7 игр, все сыграны:
   05.09 Westchester–KB Magic 89:118; 13.09 –G-Town 2 67:49; 20.09 Sparrows–W 64:69; 26.09 VSP BUZY–W 68:60;
   04.10 Rent Motors Team–W 100:80; 11.10 W–Каскад 41:50; 17.10 W–Pokrasote 57:76 (20:40 МСК).
5. Ближайший матч: **в API нет** (тур 8 не создан). Ожидаемо — выходные 24–25.10 (по правилу, ⚠ не подтверждено данными).
6. Соперник: неизвестен до жеребьёвки тура (швейцарка). Справка по любому сопернику — `tournament/4145/teams/` + `basketball_statistic/?tournament_team_id=...` + его игры по `team_id`.
7. Таблица: 28-е место из 32, 2–5.
8. Ростер: `tournament_team/31988/users/` → 19 игроков.

## Рекомендуемый способ получения

| Данные | Способ | Эндпоинт |
|---|---|---|
| Лига, сезон, турниры | API | `league/by_domain/{domain}/`, `league/{id}/tournament_seasons/`, `tournament_season/{id}/tournaments/` |
| Расписание/время/зал | API, опрос | `tournament/{id}/games/?team_id=&date_from=` |
| «Ожидаемая дата» до назначения | правило (выходные тура) + ручное подтверждение | — |
| Таблица | API | `tournament/{id}/teams/` |
| Результаты, boxscore | API | `tournament_basketball_game/{game}/user_statistic/`, `.../team_statistic/` |
| Статистика игроков/лидеры | API | `basketball_statistic/?group_by=tournament_team_user&tournament_id=...` |
| Ростер | API | `tournament_team/{id}/users/` |
| Регламент | API → PDF, вручную | `league/{id}/documents/` |
| Заявка на матч | вручную на сайте (или API с логином капитана — ⚠) | `tournament_game/{id}/game_application/` (⚠) |

## Риски

- **Неофициальный API**: нет документации и гарантий; эндпоинты извлечены из фронтенд-бандла и могут меняться.
- Времени «заранее» на сайте нет — для швейцарских дивизионов матч и соперник неизвестны до формирования тура.
- Разные имена одной команды: `team.name` «Whestchester» vs `tournament_team.name` «Westchester». Матчить по ID.
- Не хардкодить `league_id`/ID сезона: определять через `by_domain` и список сезонов (новый сезон = новые ID турниров и tournament_team).
- **Персональные данные:** API публично отдаёт даты рождения, рост, фото игроков, а в объекте `league.organization.owner`
  (в ответах сезонов/турниров) — телефон, e-mail и дату рождения владельца лиги. Не хранить лишнего, не логировать сырые ответы.
- Ответы тяжёлые (вложенные league/organization в каждом турнире) — кэшировать.
- `/basketball_statistic_leaders/` отдал 500 на подобранные параметры.
- TLS: на этой Windows-машине curl падал с `CRYPT_E_NO_REVOCATION_CHECK`, помог `--ssl-no-revoke` (особенность окружения, не сайта).

## Не удалось выяснить

- Когда именно (день/час) появляются игры следующего тура в API и есть ли до этого анонс где-то публично.
- Где капитанский чат (Telegram/WhatsApp?) и можно ли туда подключить бота.
- Правильные параметры `basketball_statistic_leaders`; содержимое `logs/`, `league/2/courts/`, `team/{id}/games/`.
- Rate limit API.
- Как устроена онлайн-заявка на матч через API (требует авторизации капитана).

## Файлы рядом

- `sample_games_team.json` — одна игра Westchester из `tournament/4145/games/?team_id=279` (logo/photo → null).
- `sample_tournament_teams.json` — 2 строки таблицы `tournament/4145/teams/`.
- `sample_roster_entry.json` — одна запись ростера `tournament_team/31988/users/`, ПДн заменены заглушками.
