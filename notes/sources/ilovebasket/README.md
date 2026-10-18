# Источник: moscow.ilovebasket.ru (МЛБЛ) — исследование для адаптера платформы

Дата исследования: 2026-10-18. Всё ниже получено реальными запросами (curl) к сайту и API в этот день (около 45 запросов, последовательно, без параллели).
Пометка «⚠ не проверено» — значит, вывод не подтверждён полученными данными.

## TL;DR

- **Лига**: в заголовке сайта она называет себя «Главная / МЛБЛ-Москва / Чемпионат Москвы по баскетболу среди любительских команд»; в регламенте — «Чемпионат МЛБЛ-Москва», федеральный сайт — ilovebasket.ru (МЛБЛ — Межрегиональная любительская баскетбольная лига, так написано в `<title>` у vlad.ilovebasket.ru).
- **Движок**: собственный сайт на ASP.NET MVC 5 (IIS 10) + AngularJS. **Все спортивные данные — из Infobasket**: открытый JSON API `https://reg.infobasket.su` (виджеты RFB/Infobasket, скрипт `old.russiabasket.ru/.../widgets.js`).
- **API открытый**: без авторизации, без капчи, без Cloudflare, отдаёт `Access-Control-Allow-Origin: *`. Rate limit не встретился (⚠ лимиты не известны — запросов было мало).
- **Сеть**: на главной ~70 поддоменов `*.ilovebasket.ru` (города и регионы). Проверены ekb, nsk, samara, krsk — тот же движок, тот же `window.comps` и тот же API `reg.infobasket.su`. **Один адаптер обслужит все такие лиги** — отличаются только ID турниров.
- **Расписание** → `GET /Comp/GetCalendar/?comps={ids}&format=json`, есть поле `HasTime` (флаг «время назначено»).
- **Ростер** → `GET /Widget/TeamRoster/{teamId}?compId={seasonId}&format=json`: ФИО, **полная дата рождения**, **позиция**, рост, вес, фото, разряд, капитан, статус допуска. Номеров в ростере нет (пусто) — номера есть только в протоколах матчей.
- **Таблица** → `GET /Widget/CompTeamResults/{stageId}?format=json`.
- **Протокол/boxscore/play-by-play** → `GET /Widget/GetOnline/{gameId}?format=json`.
- **Заявка на игру** подаётся в закрытой системе `org2.ilovebasket.ru` (с логином) — не позже чем за 15 мин до начала, максимум 12 игроков; если к началу нет 5 игроков, после 10 минут ожидания засчитывается поражение.

## 1. Платформа / движок

Заголовки `https://moscow.ilovebasket.ru/`:
```
Server: Microsoft-IIS/10.0
X-AspNetMvc-Version: 5.2
X-Powered-By: ASP.NET
Access-Control-Allow-Origin: *
Set-Cookie: ASP.NET_SessionId=...
```
- `<html ng-app="ILoveBasket">` — AngularJS; HTML-страницы содержат только шаблоны `{{...}}` и теги виджетов, **данных в HTML нет**.
- Скрипты: `/Content/html/assets/js/ILoveBasket.app.js` (контроллеры главной), `https://old.russiabasket.ru/Content/html/assets/production/js/widgets.js` (~790 КБ, виджеты RFB/Infobasket: `<competition-profile>`, `<team-profile>` и т. д.).
- Конфиг на каждой странице, прямо в inline-скрипте:
  ```js
  window.rootCompId = 37842;
  window.comps = [{"id":142698,"name":"Элита","apiUrl":"https://reg.infobasket.su","stages":[]}, ...];
  ```
  → **самый простой способ получить список дивизионов города** — скачать HTML главной и распарсить `window.comps` регуляркой.
- Особенность Windows/curl: из-за schannel нужен `--ssl-no-revoke` (иначе `CRYPT_E_NO_REVOCATION_CHECK`). В Python/requests проблемы не будет (⚠ не проверено).
- Кириллица в API — корректный UTF-8 (иногда с BOM — читать как `utf-8-sig`).
- `robots.txt` → 404 (файла нет). `sitemap.xml` → 404.

### Эндпоинты Infobasket, найденные в JS (база `https://reg.infobasket.su`)

| Эндпоинт | Что | Проверен |
|---|---|---|
| `/Widget/CompIssue/{compId}?format=json` | дерево: дивизион → этапы (`Comps[].Children`), `ParentComp` | ✅ |
| `/Comp/GetCalendar/?comps={id1,id2,...}&format=json` | календарь (работает и с ID дивизиона, и с ID этапа, и со списком через запятую) | ✅ |
| `/Comp/GetCalendarPeriods/{compId}?lang=ru&period=m` | периоды (месяцы) календаря | ✅ (332 байта) |
| `/Widget/CompTeamResults/{stageId}?format=json` | турнирная таблица + результаты каждой команды | ✅ |
| `/Widget/CrossTable/{stageId}?format=json` | шахматка | ⚠ не проверено |
| `/Widget/TeamPage/{teamId}?format=json&lang=ru` | карточка команды (названия, логотип, домашняя арена ID) | ✅ |
| `/Widget/GetTeamSeasons/{teamId}?format=json` | список сезонов команды (`CompID` сезона) | ✅ |
| `/Widget/GetTeamComps/{teamId}?season={seasonCompId}&format=json` | дивизионы команды в сезоне | ✅ |
| `/Widget/TeamGames/{teamId}?compId={seasonCompId}&format=json` | все матчи команды в сезоне | ✅ |
| `/Widget/TeamRoster/{teamId}?compId={seasonCompId}&format=json&lang=ru` | заявка (ростер) | ✅ |
| `/Widget/TeamStats/{teamId}?compId={seasonCompId}&format=json` | статистика команды и игроков | ✅ (34 КБ, детально не разбирал) |
| `/Team/GetInfo/{teamId}?format=json` | описание команды (у МИРЭА пусто: `HasData:false`) | ✅ |
| `/Widget/GamePage/{gameId}?format=json&lang=ru` | шапка матча, арена; у будущих матчей почти пусто | ✅ |
| `/Widget/GetOnline/{gameId}?format=json&lang=ru` | **протокол**: составы с номерами, boxscore, play-by-play | ✅ |
| `/Comp/GetTeamStatsForPreview/{gameId}?compId={leagueId}` | **превью матча**: средние показатели обеих команд | ✅ |
| `/Widget/PlayerPage/{personId}`, `/Widget/PlayerStats/{personId}`, `/Player/GetInfo/{personId}` | игрок | ⚠ не проверено |
| `/Comp/GetBestPlayers/{compId}`, `/Comp/GetBestTeams/`, `/Widget/BestFive/`, `/Comp/TodayBirthdays/{compId}` | лидеры, символическая пятёрка, дни рождения | ⚠ не проверено |
| `/Widget/GetOnlineStatus?games={ids}` | live-статус | ⚠ не проверено |

Картинки: логотипы `https://img.infobasket.su/logo/{id}.png`, фото `https://img.infobasket.su/photo/{id}.jpg` (URL приходят готовыми в JSON); на сайте есть прокси `/team-logo/{teamId}?apiUrl=...` и `/person-photo/{personId}?apiUrl=...`.

## 2. Структура: город → сезон → дивизион → этап → команды

```
rootCompId 37842 (общий для всех городов МЛБЛ, ⚠ смысл не проверен)
└─ Сезон 2026/2027           CompID 150000   (из GetTeamSeasons / GetTeamComps)
   └─ (Москва)               CompID 142701   (CompIDparent у дивизиона; ⚠ не запрашивал)
      └─ Лига развития (3)    CompID 142706   CompType -4, CompAbcNameRu "26/27"  ← это ID из window.comps / меню сайта
         └─ Круговой турнир   CompID 150011   CompType 0                         ← «этап», нужен для таблицы
            └─ команды (TeamID), матчи (GameID)
```
- URL сайта: дивизион `https://moscow.ilovebasket.ru/competitions/142706?apiUrl=https%3a%2f%2freg.infobasket.su`, команда `/teams/9056?apiUrl=...&compId=...`, игрок `/players/{personId}?apiUrl=...`.
- **Текущий сезон**: на сайте в `window.comps` лежат только дивизионы текущего сезона (Москва: 22 шт., у всех `CompAbcNameRu: "26/27"`); у команды — первый элемент `GetTeamSeasons` (`{"CompID":150000,"Year":2027,"SeasonName":"Сезон 2026/2027"}`). Архив — `/archive`.
- ⚠ Город может держать в `window.comps` прошлый сезон: на ekb.ilovebasket.ru — «Суперлига»/«Высшая лига» (112183/112184) с матчами 22.11.2025–10.05.2026 («25/26»), все сыграны. Значит, «текущий сезон» надо определять по датам/`CompAbcNameRu`, а не слепо по `window.comps`.

Дивизионы Москвы 2026/27 (из `window.comps`): Элита 142698, Претенденты (1) 142702, Претенденты (2) 142703, Лига Развития (1) 142704, (2) 142705, (3) 142706, (4) 142707, Lite 142699, Лига развития (4). Дружба 142708, W-Элита 142717, W-MLBL (2) 142718, Ветераны 35+ (1) 142711, 35+ (2) 142712, 40+ Дружба 142714, 45+ Дружба 142715, 50+ 142716, МЛБЛ-Дети U18 (1) 142719, U18 (2) 142720, U16 (1) 142721, U16 (2) 142722, U14 (1) 142723, U12 (1) 142725.

## 3–6. Сквозной пример: команда «МИРЭА»

**Во всех 22 дивизионах Москвы найдена одна команда с «МИРЭА»** (поиск по `МИРЭА|MIREA|РТУ` в общем календаре на 773 матча):
- TeamID **9056**, `TeamNameRu: "БК МИРЭА"`, в турнире — «МИРЭА (Москва)», дивизион **Лига развития (3)** (142706), этап «Круговой турнир» (150011).
- Сайт: https://moscow.ilovebasket.ru/teams/9056?apiUrl=https%3a%2f%2freg.infobasket.su
- У команды 11 сезонов в системе (с 2017 «REG» до 2026/2027).
- ⚠ Других «МИРЭА» (фарм, женская, ветераны) в текущих московских дивизионах нет; в других городах и в архиве не искал.

### Страница команды
HTML содержит только `<team-profile team-id="9056" api-url="https://reg.infobasket.su" initial-page="schedule" ...>`; данные виджет берёт из TeamPage + GetTeamSeasons + Team/GetInfo + GetTeamComps → TeamRoster / TeamGames / TeamStats. На странице — расписание, ростер, статистика (по составу запросов виджета).

### Расписание МИРЭА (`/Widget/TeamGames/9056?compId=150000`, 10 матчей)
```
1090089 20.09 20:00 сыгран  МИРЭА 66:48 SBA              ТМ. 4 зал
1090095 27.09 21:30 сыгран  Инвест 76:64 МИРЭА           ТМ. 4 зал
1090101 04.10 17:00 сыгран  МИРЭА 61:71 Сбер ЦА (2)      ТМ. 5 зал
1090106 11.10 12:30 сыгран  Искра (3) 77:80 МИРЭА        ТМ. 5 зал
1095970 18.10 17:00 СЕГОДНЯ IBASKET-ПАРУС – МИРЭА        ТМ. Арена   (IsToday: true)
1090115 25.10 18:30         Moscow Huskies – МИРЭА       ТМ. 4 зал
1090119 01.11 15:30         МИРЭА – Dikarshina (2)       ТМ. 5 зал
1090123 07.11 09:00         Вершина – МИРЭА              ТМ. 2 зал
1090127 14.11 10:30         МИРЭА – Hungrydogs           ТМ. 3 зал
1090133 21.11 14:50         МИРЭА – Газпромбанк          ТМ. 3 зал
```
**Время у всех матчей уже стоит (`HasTime: true`), даже на 21.11.** В дивизионе 12 команд, а матчей у МИРЭА 10 — **матча с «Московский банк» (26048) в календаре нет**. ⚠ Вероятно, он ещё не назначен — то есть «не назначенный матч» может выглядеть как отсутствие записи, а не как матч без времени.

Ближайший матч — сегодня, 18.10.2026 17:00 МСК, против **IBASKET-ПАРУС** (TeamID 80091), «ТМ. Арена». Эта команда добавлена в дивизион позже: её матчи имеют отдельные GameID (1095760…1095977) и **собственную нумерацию `GameNumber` 001–010**, пересекающуюся с основной → `GameNumber` не уникален, ключ — только `GameID`.

### Соперник: IBASKET-ПАРУС
- Таблица: 4-е место, 3–0, 190:131.
- Превью `GET /Comp/GetTeamStatsForPreview/1095970?compId=142706` → массив по обеим командам: `GameCount 3, AvgPoints "63,3", Shot3Percent "17,6", AvgRebound "64,7", AvgTurnover "21,0"...` (числа — строки с запятой).
- Ростер: 19 игроков (`/Widget/TeamRoster/80091?compId=150000`).

### Таблица Лиги развития (3) (`/Widget/CompTeamResults/150011`)
```
1 Газпромбанк 4-0 8 | 2 Инвест 3-1 7 | 3 Сбер ЦА (2) 3-1 7 | 4 IBASKET-ПАРУС 3-0 6 | 5 Moscow Huskies 2-2 6
6 Московский банк 2-2 6 | 7 МИРЭА 2-2 6 (271:272) | 8 Искра (3) 1-3 5 | 9 Вершина 1-3 5 | 10 Dikarshina (2) 1-3 5
11 Hungrydogs 1-2 4 | 12 SBA 0-4 4
```
Поля: `Place`, `TeamID`, `CompTeamName.CompTeamNameRu`, `Standings.{StandingGame, StandingWin, StandingLose, StandingPoints, StandingGoalPlus, StandingGoalMinus, StandingGoalDifference, VictoryPercent, AvgGoalPlus, AvgGoalMinus}`, `Results[]` (GameID, счёт, IsWon). Очки: 2 за победу, 1 за поражение.

### Ростер МИРЭА (`/Widget/TeamRoster/9056?compId=150000`)
16 игроков + 1 тренер, `Staff` пуст. Поля игрока:
`PersonID, PersonInfo.{PersonLastNameRu, PersonFirstNameRu, PersonSecondNameRu (отчество), PersonFullNameRu, *En, PersonBirth "дд.мм.гггг", PersonBirthday /Date()/, PersonBirthYear, PersonHeight, PersonWeight, PersonGender}, DisplayNumber, PlayerNumber, Capitan (0/1), PersonBirth, Age, PosID, Position, CountryName, Rank ("1 р." / "-"), Height, Weight, IsActive, ActiveStatus ("Допущен"), PhotoUrl`.
- ФИО — ✅ (с отчеством); **дата рождения — ✅ полная** (у всех 16); **позиция — ✅** («Разыгрывающий», «Атакующий защитник», «Лёгкий/Тяжёлый форвард», «Центровой»); рост/вес — ✅ у всех; фото — ✅ у всех 16; капитан — ✅ (1 человек).
- **Номер — ❌ пусто** у всех (`DisplayNumber: ""`, `PlayerNumber: 0`), так же у соперника. Номера появляются только в протоколе матча (`GetOnline` → `GameTeams[].Players[].DisplayNumber`).
- Английские имена бывают мусорные («New Player»).
- Образец: `samples/team_roster_9056_sample.json` (1 игрок, ДР/возраст заменены заглушками).

## 4. Время матчей и «дата без времени»

- Поля в календаре: `GameDate "18.10.2026"`, `HasTime true/false`, `GameTime "17:00"`, `GameTimeMsk`, `GameDateTime "/Date(1792332000000)/"` (= 2026-10-18T14:00Z, т. е. настоящий UTC-момент), `GameLocalDate` (полночь по МСК), `DisplayDateTimeMsk "Вс, 18 окт. 17:00 мск"`, `IsToday`, `DaysFromToday` (модуль разницы в днях — без знака!), `GameStatus` (0 — не сыгран, 1 — сыгран, 2 — идёт сейчас: видел матч в Новосибирске 18.10 со счётом 47:32), `ArenaId`, `ArenaRu`.
- **`HasTime: false` не встретился ни разу**: Москва — 773 из 773 с временем (матчи 19.09–18.12.2026), Екатеринбург — 154/154, Новосибирск — 82/82, Самара — 82/82, Красноярск — 36/36. Как сайт отображает `HasTime:false` («время уточняется» / пусто) — ⚠ не проверено (шаблоны виджетов грузятся отдельно, не нашёл).
- Разные города публикуют календарь по-разному: Москва — весь первый круг сразу с временем; **Новосибирск — только до 18.10.2026 (сегодня)**, Самара — до 26.10, Красноярск — до 25.10. То есть в регионах будущие туры, судя по всему, публикуются частями (⚠ вывод по одному снимку).
- Регламент Москвы (п. 7.2): «После опубликования календаря на официальном сайте дата и место проведения игр не могут быть изменены»; перенос времени — в рамках одной даты, по согласию команд. П. 7.8: в Едином зале Оргкомитет может менять игры местами и переносить туры, «предупредив участников не менее чем за 4 дня». П. 7.10: в Едином зале команда не может выбирать время. → для Москвы сценарий «дата есть, время позже» по данным не подтверждается; время может **меняться**, значит, адаптер должен периодически перечитывать календарь и сравнивать с прошлым снимком.
- **Зал**: только название (`ArenaRu: "ТМ. 4 зал"`, «ТМ. Арена», «ТМ. 2/3/5 зал») и `ArenaId`. `GamePage` → объект `Arena` без адреса (`ArenaNameRu`, `ArenaFullNameRu`). Адреса Единого зала на проверенных страницах сайта нет. ⚠ Что такое «ТМ» и где находится — не выяснено, вести справочник залов вручную.

## 5. Таблица, результаты, протоколы, статистика

- Таблица — `CompTeamResults/{stageId}` (см. выше). Шахматка — `CrossTable/{stageId}` (⚠ не проверено).
- Результаты — в календаре (`ScoreA/ScoreB`, `GameStatus 1`) и в `Results[]` таблицы.
- Протокол — `GetOnline/{gameId}` (442 КБ на матч 1090106): `Online` (регламент матча: 4×10, 5 фолов, `Venue2 "Москва, ТМ. 5 зал"`), `OnlineStarts` (34 записи: составы и номера), `OnlinePlays` (1212 событий play-by-play), `GameTeams[]` — **boxscore**: командные и по игрокам (`DisplayNumber`, `Points`, `Shots1/2/3`, `Rebound`, `Assist`, `Steal`, `Turnover`, `Foul`, `PlusMinus`, `PlayedTime`, `IsStart`, `PosID`, `PersonBirth`).
- У сегодняшнего (несыгранного) матча `GetOnline` пустой: `OnlineStarts 0`, `GameTeams` без игроков → **заявку соперника на игру заранее из API не увидеть** (⚠ возможно, она появляется за ~15 мин до начала, когда заявку подают в org2 — не проверено).
- Регламент: статистик загружает файл «не позднее 15:00 следующего дня» → протокол может появиться с задержкой до суток.
- Статистика игроков сезона — `TeamStats/{teamId}?compId=` (`Players[]`), лидеры — `Comp/GetBestPlayers` (⚠ не проверено).

## 7. Регламент (документ «Регламент МЛБЛ-Москва. Сезон 2026/2027»)

Список документов: https://moscow.ilovebasket.ru/documents, файл — https://moscow.ilovebasket.ru/documents/get/292 (PDF). Там же: регламент Lite (293), ветеранов (296), детей (291), Летней лиги (286), форма протокола (87).
- п. 4.1.2: число игроков в **заявке на сезон не ограничено**.
- п. 4.1.3: **на игру — не более 12 игроков**.
- п. 14.1: если к началу у команды **нет 5 игроков**, судья ждёт 10 минут, затем — поражение «лишением права».
- п. 7.11: **техническая заявка на игру — только электронно на org2.ilovebasket.ru, не менее чем за 15 минут до начала**, с номерами игроков; корректировка — у секретаря перед матчем; опоздание — технический фол тренеру.
- п. 5.5: дозаявки — с понедельника 9:00 до пятницы 12:00 МСК, иначе игрок пропускает тур. п. 5.4: дозаявки закрываются за N туров до конца регулярки (N — число игр для допуска в плей-офф; п. 4.2.2 — 6 игр).
- п. 4.1.4: регистрация команды на org2.ilovebasket.ru не позже чем за 5 дней до первой игры; фото игроков 600×600.
- Переносы (п. 7.4): позже чем за 2 недели до игры — техническое поражение 20:0.

## 8. API / экспорт / ограничения

- JSON API Infobasket — открытый, CORS `*`, без ключа. Нет документации, это внутренний API виджетов → **может измениться без предупреждения**.
- RSS: `/rss`, `/feed`, `/publications/rss` возвращают обычную HTML-страницу (200) — **RSS нет**. iCal/экспорт календаря — не найдено.
- `org2.ilovebasket.ru` — закрытая система (ASP.NET Core, форма логина, antiforgery). Подача заявок там; программный доступ — ⚠ не исследовался (нужен логин команды и согласие владельца).
- Мобильные приложения МЛБЛ (App Store / Google Play `ru.softgears.basketmlbl`), скорее всего, на том же API (⚠ не проверено).

## 9. Соцсети

На всех страницах moscow.ilovebasket.ru только **федеральные** аккаунты: https://vk.com/mlblbasket, https://t.me/mlblbasket, instagram mlblbasket, YouTube-канал. Контакт: zimin@ilovebasket.ru. Отдельных VK/Telegram МЛБЛ-Москва на сайте не найдено (⚠ могут существовать — не искал вне сайта). Новости — раздел `/publications` (HTML, без RSS). Где оперативно сообщают о смене времени — ⚠ не выяснено; по данным API время приходит сразу в календаре.

## Другие города на движке

На главной — ~70 ссылок: `22, abl, astobl, basket37, basket59, belgorod, blb, bryansk, bur, center, chel, east, ekb, firstleague, forvard, goldenring, habarovsk, hmlbl, irkbasket, irkutsk, kaluga, kamchatka, karelia, kaub, kazbas, kdbasket, kirov, kostroma, krim, krsk, kursk, kuzbass, lenobl, lyubertsy, moscow, murmansk, musson, north, nov, nsk, odin, omsk, orenburg, penza, rbl, rostov, ryazan, sakhalin, samara, saratov, siberia, south, superfinal, tambov, tmn, tver, ufa, ural, vlad, vladimir, volga, volgograd, vologda, voronezh, wam, yakutsk` + `.ilovebasket.ru`; а также внешние `52.basketball`, `tbll.ru`, `www.neva-basket.ru`, `mosbasket.ru`, `cfo/mofb/szfo.russiabasket.ru`.
Проверено: **ekb** («МЛБЛ-Екатеринбург»), **nsk** («МЛБЛ-Новосибирск»), **samara**, **krsk** — одинаковая разметка, `window.rootCompId = 37842`, `apiUrl = https://reg.infobasket.su`, календарь отдаётся тем же запросом. **vlad.ilovebasket.ru** — другая страница («Межрегиональная любительская баскетбольная лига»), без `window.comps`, со ссылкой на russiabasket.ru → не все поддомены одинаковы. Внешние домены не проверял (⚠).

## Рекомендации для адаптера

| Данные | Способ | Как |
|---|---|---|
| Список дивизионов города | HTML | `https://{city}.ilovebasket.ru/` → regex `window.comps = (\[.*?\]);` → `id`, `name`, `apiUrl` |
| Этапы дивизиона | API | `CompIssue/{divisionId}` → `Comps[]` (+`Children`, фильтр `IsPublic`) |
| Команды дивизиона | API | из `CompTeamResults/{stageId}` или уникальные `TeamAid/TeamBid` календаря |
| Расписание команды | API | `TeamGames/{teamId}?compId={seasonId}` (или общий `GetCalendar?comps=`) |
| Время | API | `HasTime` + `GameDateTime` (UTC ms); опрашивать и сравнивать снимки (diff по `GameID`) |
| Зал | API + ручной справочник | `ArenaId/ArenaRu`; адреса заполнить вручную |
| Таблица | API | `CompTeamResults/{stageId}` |
| Справка по сопернику | API | `GetTeamStatsForPreview/{gameId}?compId={leagueId}`, `TeamStats`, `Results[]` |
| Ростер | API | `TeamRoster/{teamId}?compId={seasonId}`; номера — из последнего протокола |
| Протокол / boxscore | API | `GetOnline/{gameId}` (тяжёлый, ~0,5 МБ; кешировать сыгранные навсегда) |
| Регламент | ручной | PDF в `/documents`, раз в сезон |
| Подача заявки на игру | ручной | org2.ilovebasket.ru (логин); ассистент только готовит список |

Разбор: `/Date(ms)/` → UTC; числа в статистике — строки с запятой (`"42,9"`); ответы иногда с BOM.

## Риски

1. Недокументированный API виджетов: эндпоинты/поля могут поменяться; нет SLA. Держать парсер «толерантным» и логировать изменения схемы.
2. Персональные данные: API отдаёт полные даты рождения, ФИО с отчеством, рост/вес, фото. Хранить минимум (для ассистента ДР не нужна) — 152-ФЗ.
3. «Матч без времени» в данных не встретился — неясно, как выглядит; возможно, вместо этого матч просто отсутствует (пример: МИРЭА — Московский банк). Нужна логика «ожидаемые соперники vs. опубликованные матчи».
4. `GameNumber` не уникален внутри этапа; `DaysFromToday` без знака; `window.comps` у города может указывать на прошлый сезон.
5. Адрес зала не отдаётся.
6. Заявки соперника до матча нет; протокол — с задержкой до суток.
7. Rate limit неизвестен — опрашивать умеренно (календарь раз в несколько часов, протоколы — один раз после матча).

## Что не удалось выяснить

- Как UI показывает `HasTime:false` и бывает ли это вообще в МЛБЛ.
- Где публикуются оперативные изменения времени (VK/Telegram Москвы, чаты капитанов).
- Адрес «ТМ» (Единого зала).
- Ответы `CrossTable`, `PlayerPage`, `GetBestPlayers`, `GetOnlineStatus`; смысл `rootCompId 37842` и ID 142701.
- Доступ к org2 (API подачи заявки).

## Образцы

- `samples/team_games_9056_sample.json` — 3 матча МИРЭА (сыгранный, сегодняшний, будущий).
- `samples/standings_150011_sample.json` — 2 строки таблицы Лиги развития (3).
- `samples/team_roster_9056_sample.json` — 1 игрок + тренер, ДР/возраст заменены заглушками (01.01.2000, Age 0).
