# Боты ВКонтакте (VK API): возможности для basket-assist

Дата исследования: 2026-10-18. Источник: официальная документация dev.vk.com (страницы скачивались напрямую 18.10.2026; канонический домен в метаданных страниц уже `dev.vk.ru`, хост API в примерах `api.vk.ru`).

Обозначения:
- ✅ **проверено по докам**: утверждение есть в тексте официальной страницы, ссылка рядом.
- 🔶 **вывод**: прямо в доках не написано, но следует из нескольких проверенных фактов. Перед использованием нужна практическая проверка.
- ⚠ **не проверено**: в официальных источниках подтверждения не нашлось.

Сторонние источники использовались только как подсказка, где искать. Они помечены «(сторонний)».

---

## 0. Актуальная версия API

- ✅ Последняя версия в списке изменений — **5.199** («Изменения во внутренних алгоритмах… Изменений в составе API-методов… нет»). Выше по списку ничего нет: [reference/versions](https://dev.vk.com/ru/reference/versions).
- ✅ Документация противоречит сама себе. На странице «Формат запросов» написано «Актуальная версия API — 5.131», и почти все примеры в доках (боты, Callback, Long Poll) тоже на 5.131: [api/api-requests](https://dev.vk.com/ru/api/api-requests).
- ✅ Обязательный параметр `v` нужен в каждом запросе (там же). Сервер принимает любые значения: при проверке 18.10.2026 `utils.getServerTime` ответил и на `v=5.199`, и на `v=5.260`, и на `v=6.0`. Поэтому узнать «настоящую текущую» версию запросом к API нельзя.
- 🔶 Для MVP берём **5.199**: это последняя версия по changelog, и её же использует vkbottle (см. п. 11).
- ✅ Чтобы получать события из бесед, версия API callback-сервера должна быть **не ниже 5.80**: [api/bots/getting-started](https://dev.vk.com/ru/api/bots/getting-started). Callback поддерживает версии от 5.81: [api/callback/getting-started](https://dev.vk.com/ru/api/callback/getting-started).

---

## 1. Что нужно для бота

**Ответ:** нужны сообщество и ключ доступа сообщества с правами `messages` (а для Long Poll ещё `manage`). Бот на токене пользователя для нового проекта невозможен.

- ✅ Для бота нужны «Сообщество, от имени которого бот будет общаться с пользователями», сервер и сценарии. «Для подключения чат-бота можно использовать любое сообщество ВКонтакте»: [api/bots/getting-started](https://dev.vk.com/ru/api/bots/getting-started).
- ✅ **Шаги** (там же и на [keyboard/enable-keyboard](https://dev.vk.com/ru/api/bots/development/keyboard/enable-keyboard)):
  1. Создать сообщество (тип любой) и оформить описание.
  2. «Управление → Сообщения»: включить **Сообщения сообщества**.
  3. «Управление → Сообщения → Настройки для бота»: **Возможности ботов → Включены**. Здесь же можно добавить кнопку «Начать».
  4. Там же включить **«Разрешать добавлять сообщество в чаты»** (см. п. 3).
  5. «Управление → Дополнительно → Работа с API → Ключи доступа → Создать ключ», отметить права. Действие подтверждается в мобильном приложении VK или по SMS: [access-token/community-token](https://dev.vk.com/ru/api/access-token/community-token).
  6. Включить Long Poll API (вкладка Long Poll API → «Включено») и отметить типы событий, либо настроить Callback API (п. 2).
- ✅ Ключ сообщества может получить только администратор сообщества. Срок действия ключа не ограничен, ключ можно отозвать. Ключей может быть несколько, с разными правами: [access-token/community-token](https://dev.vk.com/ru/api/access-token/community-token).
- ✅ **Права (scope) ключа сообщества:** `stories`, `photos`, `app_widget`, `messages` («Доступ к сообщениям сообщества»), `docs`, `manage` («Доступ к администрированию сообщества»): [reference/access-rights](https://dev.vk.com/ru/reference/access-rights).
  - ✅ `messages` нужен для `messages.send`, `messages.edit`, `messages.pin`, `messages.getConversationMembers`, `messages.isMessagesFromGroupAllowed`, `messages.sendMessageEventAnswer` (см. страницы методов ниже).
  - ✅ `manage` нужен для `groups.getLongPollServer`: «Ключи доступа сообщества, не включающие этот scope, использовать для вызова метода не получится»: [method/groups.getLongPollServer](https://dev.vk.com/ru/method/groups.getLongPollServer).
  - 🔶 Минимальный набор для MVP: **`messages` + `manage`**. `photos` и `docs` понадобятся, только если бот будет отправлять картинки или файлы.
- ✅ **Бот без сообщества (на токене пользователя) не подходит.** Право `messages` для пользовательского ключа «можно получить… если вы запрашивали его ранее. **Для новых приложений это право не выдаётся**»: [reference/access-rights](https://dev.vk.com/ru/reference/access-rights). На страницах всех методов `messages.*` пользовательский вариант помечен: «выдаётся в исключительных случаях через запрос в поддержку» ([messages.send](https://dev.vk.com/ru/method/messages.send)). Вся документация по ботам и [Правила для ботов](https://dev.vk.com/ru/bots-rules) написаны про бота сообщества.
- ⚠ Прямого запрета вида «нельзя делать бота на аккаунте пользователя» в правилах я не нашёл. Но без права `messages` технически это не сделать, а «userbot» на чужом аккаунте противоречит п. 4.2 [Правил платформы](https://dev.vk.com/ru/rules) («Приложение не должно вызывать какие-либо активные действия в API без ведома пользователей»). Это моя интерпретация.

---

## 2. Получение событий: Callback API или Bots Long Poll API

**Ответ:** для MVP на домашнем ПК или VPS без домена удобнее **Bots Long Poll**: не нужны публичный адрес и сертификат. Callback имеет смысл позже, когда будет VPS с доменом и HTTPS.

**Bots Long Poll API** ([api/bots-long-poll/getting-started](https://dev.vk.com/ru/api/bots-long-poll/getting-started)):
- ✅ «Очередь из событий хранится на стороне ВКонтакте». Сервер бота сам делает запросы к VK, поэтому входящий публичный адрес не нужен (🔶 вывод из схемы работы).
- ✅ Включается в «Управление → Дополнительно → Работа с API → Long Poll API → Включено». Типы событий отмечаются там же или через `groups.setLongPollSettings`.
- ✅ Схема: `groups.getLongPollServer` выдаёт `server`, `key`, `ts`, затем идут запросы `{server}?act=a_check&key=…&ts=…&wait=25`. Рекомендуемый `wait` — 25, максимум — 90.
- ✅ Ошибки `failed: 1` (взять новый ts), `failed: 2` (новый key), `failed: 3` (новые key и ts).
- ✅ Структура событий такая же, как в Callback API.

**Callback API** ([api/callback/getting-started](https://dev.vk.com/ru/api/callback/getting-started)):
- ✅ VK сам отправляет POST с JSON на ваш адрес. Адрес подтверждается: на событие `confirmation` сервер возвращает заданную строку. На каждое событие нужно ответить строкой `ok` с HTTP 200. Если сервер несколько раз подряд вернёт ошибку, VK временно перестанет слать уведомления.
- ✅ Можно подключить до 10 серверов, у каждого свои события и версия API. Есть поле «Секретный ключ» (`secret` в каждом уведомлении).
- ✅ Повторы при сбое: через 10 с, 3 мин, 10 мин, 30 мин, 1 ч (заголовок `X-Retry-Counter`). Поддерживаются `Retry-After` с кодами 410/429/503.
- ✅ SSL: «мы рекомендуем загрузить SSL-сертификат». В разделе про аутентификацию по клиентскому сертификату сказано: «Серверный сертификат должен быть выдан авторизованным центром сертификации… Самоподписанного корневого сертификата недостаточно» (пример — Let's Encrypt).
- 🔶 Публичный адрес, доступный из интернета, нужен по самой схеме: VK отправляет на него запросы. С домашнего ПК это значит белый IP, проброс порта или туннель.
- ⚠ Обязателен ли HTTPS (или можно http) и есть ли таймаут на ответ, прямо не сказано.

**Для MVP:** Long Poll. Работает с домашнего ПК за NAT и с дешёвого VPS без домена. Нужно не забыть право `manage` в ключе.

---

## 3. Беседы (чаты)

**Можно ли добавить бота в беседу, созданную пользователем**
- ✅ «Чтобы пользователи могли добавлять бота в чаты, … Управление → Сообщения → Настройки для бота → **Разрешать добавлять сообщество в чаты**. На странице сообщества появится кнопка добавления бота в чат»: [api/bots/getting-started, шаг 4](https://dev.vk.com/ru/api/bots/getting-started).
- 🔶 Ограничений на то, кем создана беседа, в доках нет, значит обычная пользовательская беседа подходит.
- ⚠ Кто именно может добавить бота, отдельно не описано. 🔶 Вероятно, это зависит от настройки беседы `only_admins_invite` («только администраторы могут приглашать», см. `chat_restrictions` в [messages.getConversationMembers](https://dev.vk.com/ru/method/messages.getConversationMembers)). Если она включена, добавлять бота должен администратор беседы.

**Что бот видит: три уровня доступа** ([api/bots/getting-started](https://dev.vk.com/ru/api/bots/getting-started)) ✅
| Доступ | Кто выдаёт | Что может бот |
|---|---|---|
| Только упоминания | выдаётся сразу после добавления | писать в чат; видеть упоминания и ответы на сообщения бота |
| Полный | любой администратор чата | писать; видеть **все** сообщения |
| Администратор | создатель чата или админ с правом назначать админов | всё перечисленное + получать объект [Беседа](https://dev.vk.com/ru/reference/objects/conversation) + **получать участников чата** |

- ✅ Ещё бот видит сообщения, которые «начинаются с символа `/`»: [bots/development/messages/management](https://dev.vk.com/ru/api/bots/development/messages/management).
- 🔶 Нам нужен уровень **«Администратор»**: только он даёт список участников (п. 7), и он же, скорее всего, нужен для закрепления (п. 8). Беседу создал игрок или капитан, он же создатель и может выдать этот доступ.

**peer_id и идентификация**
- ✅ `peer_id` групповой беседы = `2000000000 + id беседы`, пользователя = его id, сообщества = `-id`: [messages.send](https://dev.vk.com/ru/method/messages.send).
- ✅ В объекте сообщения есть `peer_id` и `from_id` (автор): [reference/objects/message](https://dev.vk.com/ru/reference/objects/message). По `from_id` видно, кто написал. Имена можно получить через `users.get`, он доступен с ключом сообщества: [method/users.get](https://dev.vk.com/ru/method/users.get).

**Как бот узнаёт, что его добавили**
- ✅ Служебные сообщения содержат `action.type`: `chat_invite_user`, `chat_invite_user_by_link`, `chat_kick_user`, `chat_pin_message` и др., плюс `action.member_id`: [reference/objects/message](https://dev.vk.com/ru/reference/objects/message). Они приходят как `message_new` ([community-events/json-schema](https://dev.vk.com/ru/api/community-events/json-schema)).
- 🔶 Добавление самого бота — это `message_new` с `action.type = chat_invite_user` и `action.member_id = -<id сообщества>`.
- ⚠ В доках `member_id < 0` описан как «email», про сообщества там ничего нет. Формат нужно проверить на реальном событии.
- ⚠ Приходит ли боту `message_new` о собственном добавлении, если у него доступ «только упоминания», документация не говорит. По практике сторонних разработчиков приходит, но это не проверено.

---

## 4. Кнопки: клавиатуры и callback-кнопки

**Виды клавиатур** ([bots/development/keyboard](https://dev.vk.com/ru/api/bots/development/keyboard)) ✅
- Обычная («клавиатура чата», `inline: false`): под полем ввода, **до 40 кнопок, до 10 рядов по 5**; `one_time` скрывает её после нажатия.
- Inline (`inline: true`): внутри сообщения, **до 10 кнопок, до 6 рядов по 5**.
- Цвета `primary` / `secondary` / `negative` / `positive` есть только у кнопок `text` и `callback`.
- «Пользователь может подменить payload вручную», поэтому payload нужно проверять на сервере.

**Типы кнопок** ([keyboard/key-types](https://dev.vk.com/ru/api/bots/development/keyboard/key-types)) ✅
- `text`: отправляет в чат сообщение с текстом `label`. `label` до **40 символов**, `payload` до **255 символов** (JSON-строка в `{}`).
- `callback`: «обрабатывать нажатия без отправки пользователем сообщения в чат». На сервер приходит событие `message_event`. `label` до 40 символов, `payload` до 255.
- `open_link`, `open_app`, `location`, `vkpay`.

**Callback-кнопки** ([keyboard/callback-keys](https://dev.vk.com/ru/api/bots/development/keyboard/callback-keys), [messages.sendMessageEventAnswer](https://dev.vk.com/ru/method/messages.sendMessageEventAnswer), [community-events/json-schema#message_event](https://dev.vk.com/ru/api/community-events/json-schema)) ✅
- Схема: нажатие → событие `message_event` (Callback или Long Poll) → бот вызывает `messages.sendMessageEventAnswer` с `event_data` → клиент выполняет действие.
- В `message_event` есть **`user_id`** (кто нажал), `peer_id`, `event_id`, `payload` и `conversation_message_id`. **Нажавший известен.**
- `event_id` действует одну минуту.
- `conversation_message_id` «Не передаётся для клавиатур беседы». 🔶 Отсюда следует, что callback-кнопки поддерживаются и в беседах. Для inline-клавиатуры cmid приходит, и сообщение можно отредактировать (п. 8).
- Действия ответа: `show_snackbar` (до 90 символов, держится до 10 с), `open_link`, `open_app`.
- 🔶 Снекбар адресуется по `user_id` и, судя по всему, виден только нажавшему. Это удобно для подтверждения «Отмечено: буду».
- Пока нет ответа, на кнопке крутится индикатор ожидания.
- Метод `sendMessageEventAnswer` работает только с ключом сообщества (`messages`).

**Поддержка клиентов** ✅
- В каждом `message_new` (API ≥ 5.103) приходит `client_info`: `button_actions` (в том числе `callback`), `keyboard`, `inline_keyboard`, `carousel`. Доки предупреждают: часть функций может быть недоступна на разных платформах, версиях и в сторонних клиентах. Если клавиатура недоступна, советуют дать нумерованные варианты текстом: [api/bots/getting-started](https://dev.vk.com/ru/api/bots/getting-started).
- ⚠ Таблицы «веб / iOS / Android поддерживают callback-кнопки в беседах» в доках нет.
- ⚠ Приходит ли `message_event`, когда у бота в беседе доступ «только упоминания», не сказано. 🔶 Для надёжности выдаём боту доступ «Администратор».
- ⚠ Как ведёт себя нажатие `text`-кнопки в беседе при доступе «только упоминания» (добавляется ли упоминание бота автоматически), не документировано.

---

## 5. Нативные опросы VK или callback-кнопки

- ✅ `polls.create` можно вызвать **только с ключом пользователя** (Implicit Flow или VK Mini Apps; право `wall` «выдаётся в исключительных случаях»). Ключа сообщества в списке нет. Опрос создаётся для прикрепления «к записям на странице пользователя или сообщества». Для сообщества передаётся `owner_id = -group_id`. Есть параметры `is_anonymous` (по умолчанию 0, то есть публичный) и до 10 вариантов: [method/polls.create](https://dev.vk.com/ru/method/polls.create).
- ✅ `polls.getVoters` (кто как проголосовал) тоже работает **только с ключом пользователя**. В анонимном опросе «список проголосовавших недоступен». Есть ошибка 253 «Access denied, please vote first»: [method/polls.getVoters](https://dev.vk.com/ru/method/polls.getVoters). `polls.getById` также только для ключа пользователя: [method/polls.getById](https://dev.vk.com/ru/method/polls.getById).
- ✅ Вложение `poll` в `messages.send` поддерживается: [messages.send](https://dev.vk.com/ru/method/messages.send). Но создать опрос от имени сообщества ключом сообщества по докам нельзя.
- ✅ Событие `poll_vote_new` («Добавление голоса в публичном опросе») содержит `owner_id`, `poll_id`, `option_id`, `user_id`: [community-events/json-schema](https://dev.vk.com/ru/api/community-events/json-schema).
- ⚠ Приходит ли `poll_vote_new` за голоса в опросе, прикреплённом в беседе (а не на стене сообщества), не документировано.
- ⚠ Можно ли создать опрос руками в веб-интерфейсе от имени сообщества и прикрепить его в беседу ботом, не проверено.

**Сравнение для нашей задачи**
| Критерий | Нативный опрос | Callback-кнопки (inline) |
|---|---|---|
| Создание ботом (ключ сообщества) | ✅ нет, нужен ключ пользователя | ✅ да, `messages.send` + `keyboard` |
| Кто ответил | только `polls.getVoters` с ключом пользователя ✅; событие — ⚠ | `user_id` в каждом `message_event` ✅ |
| Один тап | да | да, сообщение в чат не отправляется ✅ |
| Изменить ответ | зависит от `disable_unvote` | бот сам хранит последний выбор |
| Обновлять счётчик «идут: 7» | VK показывает сам | бот редактирует сообщение (`messages.edit`) |
| Привязка к логике (заявка на сезон, сводки) | слабая | полная |

**Вывод:** берём **inline-клавиатуру с тремя callback-кнопками** «Буду / Не буду / Под вопросом» под сообщением-анонсом. Бот отвечает снекбаром и редактирует анонс со счётчиком. Нативные опросы ботом сообщества по докам не реализуемы.

---

## 6. Личные сообщения: может ли бот написать первым

**Ответ: нет, пока пользователь сам не разрешил.** Для напоминаний это ключевое ограничение.

- ✅ «Чтобы вы могли отправлять пользователю сообщения от имени сообщества, пользователь должен разрешить их получение. Если пользователь написал сообщение сообществу первым, это приравнивается к согласию… (без ограничений по времени, если пользователь не запретил сообщения вручную)»: [api/community-messages/getting-started](https://dev.vk.com/ru/api/community-messages/getting-started).
- ✅ Официальные способы запросить разрешение (там же):
  - событие VK Bridge `VKWebAppAllowMessagesFromGroup` в мини-приложении; доступно только после модерации приложения ([bridge/VKWebAppAllowMessagesFromGroup](https://dev.vk.com/ru/bridge/VKWebAppAllowMessagesFromGroup));
  - метод `messages.allowMessagesFromGroup`: только ключ пользователя с `messages`, то есть для нас недоступен ([method](https://dev.vk.com/ru/method/messages.allowMessagesFromGroup));
  - виджет «Разрешить сообщения от сообщества» на внешнем сайте ([widgets/allow-messages-from-community](https://dev.vk.com/ru/widgets/allow-messages-from-community)).
- ✅ События `message_allow` («Пользователь нажал кнопку Разрешить сообщения **или написал первое сообщение** сообществу», с `user_id` и `key`) и `message_deny`: [community-events/json-schema](https://dev.vk.com/ru/api/community-events/json-schema).
- ✅ Проверка: `messages.isMessagesFromGroupAllowed(group_id, user_id)` возвращает `is_allowed` 0/1, нужен ключ сообщества с `messages`: [method](https://dev.vk.com/ru/method/messages.isMessagesFromGroupAllowed).
- ✅ «Через API нельзя получить список всех пользователей, разрешивших сообщения сообществу. Необходимо хранить и синхронизировать этот список на своей стороне»: [community-messages/getting-started](https://dev.vk.com/ru/api/community-messages/getting-started).
- ✅ Ошибки отправки: 901 «Can't send messages for users without permission», 902 (приватность), 900 (чёрный список), 987 «Need message request», 1021 «without conversation»: [messages.send](https://dev.vk.com/ru/method/messages.send).
- ✅ Ссылки `vk.me/{group}?ref=…&ref_source=…` и `vk.com/write-{group_id}?ref=…` открывают диалог с сообществом. `ref` возвращается в `message_new`: [community-messages/getting-started](https://dev.vk.com/ru/api/community-messages/getting-started).
- 🔶 **Как получить разрешение прямо из беседы:** бот публикует в беседе кнопку `open_link` на `vk.me/<сообщество>?ref=<peer_id беседы>`. Игрок переходит, нажимает «Начать» (если кнопка включена в настройках) или пишет любое сообщение. Это «написал первым», то есть согласие, и бот получает `message_new` с `ref` и `message_allow`. Так же игрок может нажать «Разрешить сообщения» на странице сообщества. Схема собрана из документированных частей, на практике не проверена.
- ⚠ Кнопки, которая выдаёт разрешение прямо из беседы, в один тап и без перехода в диалог, в доках нет.
- ⚠ Смысл ошибки 987 «Need message request» (появились ли «запросы на переписку») не описан.
- 🔶 Разрешение выдаётся **сообществу**. Если один бот обслуживает все команды, игрок разрешает один раз, и это работает для всех его команд.

---

## 7. Участники беседы: messages.getConversationMembers

- ✅ Ключ сообщества с правом `messages`. Параметры `peer_id`, `extended`, `fields`. Возвращает `items` (`member_id`, `invited_by`, `join_date`, `is_admin`, `can_kick`), `chat_restrictions`, а также **`profiles`** (объекты пользователей с именами) и `groups`. Ошибка 917 «You don't have access to this chat»: [method/messages.getConversationMembers](https://dev.vk.com/ru/method/messages.getConversationMembers).
- ✅ «Получать участников чата» может только бот с доступом **«Администратор»**: [api/bots/getting-started](https://dev.vk.com/ru/api/bots/getting-started).
- ✅ Имена есть в `profiles` (объект пользователя содержит `first_name`, `last_name`). Отдельно имя по id выдаёт `users.get` с ключом сообщества.
- 🔶 Без прав администратора бот узнаёт игроков только по тем, кто нажал кнопку или написал. Для голосования этого хватает, а вот список «кто ещё не ответил» без прав администратора не построить.

---

## 8. Редактирование и закрепление сообщений

- ✅ `messages.edit` (ключ сообщества, `messages`): `peer_id` + `cmid` (или `message_id`), новый `message` (**до 4096 символов**) и `keyboard`. Ошибки: 909 «too old», 949 «Can't edit pinned message yet», 920 и др.: [method/messages.edit](https://dev.vk.com/ru/method/messages.edit).
- ✅ Для редактирования в беседе берут `conversation_message_id`. Его можно получить сразу из `messages.send` с параметром `peer_ids` (API ≥ 5.124): [messages/management](https://dev.vk.com/ru/api/bots/development/messages/management). Ответ `messages.send` с `peer_ids` возвращает `peer_id`, `message_id`, `conversation_message_id`: [messages.send](https://dev.vk.com/ru/method/messages.send).
- ✅ Событие `message_edit` приходит только для сообщений бота: [community-events/json-schema](https://dev.vk.com/ru/api/community-events/json-schema).
- ✅ `messages.pin` (`peer_id`, `conversation_message_id`) и `messages.unpin` работают с ключом сообщества. Ошибки 925 «You are not admin of this chat», 1014 «Max count of pinned messages reached»: [method/messages.pin](https://dev.vk.com/ru/method/messages.pin), [method/messages.unpin](https://dev.vk.com/ru/method/messages.unpin).
- 🔶 Для закрепления бот, видимо, должен быть администратором беседы (ошибка 925, флаг `only_admins_edit_pin`).
- ⚠ Через сколько времени сообщение становится «too old» (ошибка 909), не сказано. Обновлять счётчик неделю подряд может не получиться.
- ⚠ Ошибка 949 говорит, что закреплённое сообщение, возможно, нельзя редактировать сразу. Условия не описаны.
- 🔶 Схема: анонс + inline-кнопки → бот запоминает `cmid` → при каждом голосе вызывает `messages.edit` с новым текстом «Идут: 7 / Не идут: 2 / Под вопросом: 3» и той же клавиатурой. Если `edit` вернул 909, бот отправляет новое сообщение.

---

## 9. Лимиты

- ✅ Ключ сообщества: **20 запросов в секунду** (ключ пользователя — 3/с). При превышении приходит ошибка 6. Существуют и количественные ограничения на однотипные методы: «Мы не предоставляем информацию о точных лимитах». При их превышении метод временно недоступен или требует капчу: [api/api-requests](https://dev.vk.com/ru/api/api-requests), [bots/development/messages](https://dev.vk.com/ru/api/bots/development/messages). Ошибка 9 — «Слишком много однотипных действий»: [reference/errors](https://dev.vk.com/ru/reference/errors).
- ✅ **Новое: месячные лимиты с 7 сентября 2026.** «Для одного аккаунта в Кабинете для бизнеса VK ID… **без верификации бизнес-профиля — 10 000 вызовов в месяц**; после верификации — 100 000 000». Там же: «Методы, не входящие в публичную документацию, будут поэтапно ограничены» ([Правила платформы, редакция от 19.08.2026](https://dev.vk.com/ru/rules)).
  - ⚠ **Распространяется ли лимит на ключ сообщества, созданный в настройках сообщества (без приложения в VK ID), не подтверждено.** Страница «Условий использования VK API» (id.vk.ru/…/tariffs) не загрузилась без JS. Если распространяется, 10 000 вызовов в месяц хватит одной команде, но не сотням. Тогда нужна верификация бизнес-профиля.
  - 🔶 Long Poll-запросы `a_check` идут на отдельный сервер, а не на `/method/`. Считаются ли они вызовами, неизвестно. ⚠ не проверено.
- ✅ Рассылка: `messages.send` с `peer_ids` (до 100 получателей, только для ключа сообщества) отправляет один текст многим. `user_ids` устарел (deprecated с 5.138): [messages.send](https://dev.vk.com/ru/method/messages.send), [reference/versions](https://dev.vk.com/ru/reference/versions).
- ✅ `random_id` защищает от дублей: уникальность проверяется в диалоге за последний час, но не дальше 100 последних сообщений: [messages.send](https://dev.vk.com/ru/method/messages.send).
- ✅ Размер сообщения: `messages.send` — до **9000** символов, `messages.edit` — до **4096**. Ошибка 914 «Message is too long»: [messages.send](https://dev.vk.com/ru/method/messages.send), [messages.edit](https://dev.vk.com/ru/method/messages.edit). 🔶 Сообщение, которое потом будем редактировать, держим короче 4096.
- ✅ Сложных POST-параметров больше 2 КБ передаём как `multipart/form-data`; `application/json` не поддерживается: [api/api-requests](https://dev.vk.com/ru/api/api-requests).
- ⚠ Метод `execute` (пакет вызовов) упоминается в описании ошибок, но страница метода на dev.vk.com отдала пустой шаблон. Лимит «до 25 вызовов» известен только из сторонних источников.

---

## 10. Подводные камни мультикомандного сервиса и правила

**Технические**
- ✅ Один бот сообщества может состоять во многих беседах, и каждая беседа — отдельный `peer_id` (п. 3). 🔶 Ключ хранения — `peer_id` беседы → команда. Привязку делать по событию добавления в беседу или командой `/start` от капитана.
- ✅ В каждой беседе доступ боту выдаёт её администратор (п. 3). 🔶 Значит, в онбординг капитана входит шаг «выдать боту права администратора».
- ✅ Список разрешивших ЛС через API не получить, его надо хранить самим (п. 6).
- ✅ Лимит 20 запросов в секунду общий на ключ сообщества (п. 9). 🔶 При сотнях бесед нужна очередь отправки с ограничением частоты. Напоминания лучше разносить по времени, а итоговые сводки группировать.
- ✅ Месячный лимит вызовов (п. 9). Если он применяется к нашему ключу, понадобится верификация бизнес-профиля.
- ✅ В `messages.send` есть новые ошибки **993 «Chat moved to MAX»**, 994 «Chat forward disabled», 995 «Messenger degradation», 996 «Chat blocked due to response from another admin»: [messages.send](https://dev.vk.com/ru/method/messages.send). ⚠ Описания нет. 🔶 Похоже, беседы VK могут переезжать в мессенджер MAX, и тогда бот VK теряет доступ к беседе. Это риск, и вместе с ним стоит проверить и MAX как канал. Сторонний поиск официального объявления не дал.
- ✅ В Правилах платформы (п. 1.8) VK оставляет за собой право «перенести ваше Приложение на другую нашу платформу»: [dev.vk.com/ru/rules](https://dev.vk.com/ru/rules).

**Правила для ботов** ([bots-rules](https://dev.vk.com/ru/bots-rules)) ✅
- Работа бота не должна зависеть от подписки на сообщество, лайков и репостов (п. 1). Нельзя требовать «вступи в сообщество, чтобы пользоваться».
- Обещанную функцию бот выполняет в течение суток. Если функция отложенная (например, «запланированное напоминание»), бот в ответ на команду сообщает время выполнения (п. 2, 2.2).
- Если бот не работает по техническим причинам, это объявляется на видном месте в сообществе (п. 2.1).
- **Рекламные сообщения и рассылки в многопользовательские беседы запрещены**, за нарушение отключат сообщения сообщества (п. 7.1). У нас только служебные анонсы, но рекламу в беседы не добавлять никогда.
- Названия команд должны честно описывать действие (п. 4).

**Правила платформы, работа с данными** ([dev.vk.com/ru/rules](https://dev.vk.com/ru/rules), ред. 19.08.2026) ✅
- 2.1: нельзя собирать и хранить данные, включая User ID, в целях, не связанных с функционированием приложения. Хранить «на всякий случай» нельзя.
- 2.2: нельзя передавать данные, полученные через API (включая User ID), сторонним сервисам.
- 2.3: нельзя использовать данные в рекламе.
- 2.4: нельзя использовать данные для аналитических отчётов и перепродажи без согласия VK.
- 4.1: оповещения без спама и посторонней информации. 4.2: никаких действий без ведома пользователя.
- 4.6: при «необоснованной частотности обращений» или избыточном сборе данных VK может ограничить доступ без уведомления.
- Использовать можно только методы из публичной документации.
- 🔶 Для нас: храним только `user_id`, имя, ответы и связь с командой. Передавать капитану можно (это функция сервиса). В сторонние сервисы (аналитику, LLM) User ID не отправляем.
- ⚠ Требования 152-ФЗ (персональные данные, политика обработки, согласие) в доках VK не описаны. Это отдельный юридический вопрос, не проверен.

---

## 11. Python-библиотеки

Источник — PyPI и содержимое пакетов (не dev.vk.com), проверено 18.10.2026.
- ✅ **vkbottle** — последний релиз **4.11.0 от 26.08.2026** (до этого 4.10.0 — 24.06.2026, 4.9.0 — 03.06.2026), Python `>=3.10,<4.0`, то есть 3.13 подходит. Версия API берётся из `vkbottle-types` 5.199.99.22 (26.08.2026): `API_VERSION = "5.199"`, `API_URL = "https://api.vk.ru/method/"`. Это совпадает с последней версией из changelog. Есть Bots Long Poll, callback-кнопки, клавиатуры: [pypi.org/project/vkbottle](https://pypi.org/project/vkbottle/), [github.com/vkbottle/vkbottle](https://github.com/vkbottle/vkbottle).
- ✅ **vk_api** (python273) — 11.10.1 от 17.07.2026, до этого был перерыв 2022→2025. Уже ходит на `api.vk.ru`, но по умолчанию `api_version='5.92'`, поэтому версию нужно задавать явно. Синхронная библиотека, есть `VkBotLongPoll`: [pypi.org/project/vk-api](https://pypi.org/project/vk-api/).
- ✅ **vkwave** — 0.2.17 от 15.05.2022, заброшена.
- 🔶 Рекомендация: **vkbottle** (async, актуальна, 5.199). Работу с VK закрываем своим `ChannelAdapter`, как требует CLAUDE.md, чтобы при переезде бесед в MAX (п. 10) поменять только адаптер.

---

## Выводы для MVP

### Рекомендуемая схема
1. **Сообщество VK** «basket-assist» (одно на весь сервис), в нём бот. Ключ сообщества с правами `messages` + `manage`.
2. **Беседа команды** остаётся та же, созданная пользователем. Бота добавляет создатель или админ и выдаёт ему доступ **«Администратор»**: нужен список участников, закрепление и, для надёжности, приём всех событий.
3. **Получение событий: Bots Long Poll API** (vkbottle). Работает с домашнего ПК или VPS без домена и сертификата. На Callback API переходим, когда появится VPS с HTTPS.
4. **Опрос:** анонс матча с **inline-клавиатурой из трёх callback-кнопок** «✅ Буду / ❌ Не буду / ❓ Под вопросом». `message_event` даёт `user_id` → бот сохраняет ответ → `sendMessageEventAnswer` со снекбаром «Записал: буду» → `messages.edit` анонса со счётчиком «Идут: 7 · Не идут: 2 · ?: 3». Анонс можно закрепить (`messages.pin`). Запасной вариант на случай, когда `client_info` без `callback` или inline: текстовые команды `/буду`, `/небуду`, `/?` (сообщения на `/` бот видит).
5. **Напоминания в ЛС** только тем, кто разрешил сообщения. В беседе бот публикует кнопку-ссылку `vk.me/<сообщество>?ref=<peer_id>` «Получать напоминания». Игрок нажимает «Начать» в диалоге и тем самым даёт согласие (`message_allow`). Перед отправкой проверяем `isMessagesFromGroupAllowed`. Если игрок не разрешил, упоминаем его в беседе.
6. **Капитану** отправляем итоговый список и сводки за 24 ч и за 2 ч, в ЛС (капитан тоже должен разрешить сообщения) или в беседу.

### Шаги настройки для владельца команды (MVP)
1. Создать сообщество VK (любой тип, можно закрытое).
2. Управление → Сообщения: включить «Сообщения сообщества».
3. Управление → Сообщения → Настройки для бота: «Возможности ботов» → Включены; поставить галочку «Добавить кнопку „Начать“»; включить «Разрешать добавлять сообщество в чаты».
4. Управление → Дополнительно → Работа с API → Ключи доступа: создать ключ с правами `messages` и `manage` (управление сообществом), подтвердить по SMS или в приложении, передать ключ в конфиг сервиса (не публиковать).
5. Там же, вкладка Long Poll API: «Включено», версия API 5.199. В «Типах событий» отметить: входящее сообщение (`message_new`), действие с сообщением (`message_event`), разрешение и запрет сообщений (`message_allow` / `message_deny`), редактирование (`message_edit`) — по необходимости.
6. На странице сообщества нажать кнопку добавления в чат и выбрать беседу команды (или добавить сообщество в беседу из самой беседы).
7. В настройках беседы создатель выдаёт боту доступ «Администратор».
8. Каждый игрок один раз открывает диалог с сообществом (по кнопке из беседы) и нажимает «Начать». Это нужно для напоминаний в ЛС.

### Риски
- **Месячный лимит вызовов API** (10 000 в месяц без верификации бизнес-профиля, с 07.09.2026). Неясно, касается ли он ключа сообщества. Для одной команды, скорее всего, хватит, для сотен — нет.
- **Переезд бесед в MAX** (ошибка 993 «Chat moved to MAX»). Беседа может «уехать», и бот VK её потеряет. Нужны обработка ошибки и уведомление капитана; MAX — следующий канал для исследования.
- **ЛС без согласия невозможны.** Часть игроков не нажмёт «Начать», поэтому нужен запасной вариант с упоминанием в беседе.
- **Права в беседе** выдаёт человек. Без доступа «Администратор» нет списка участников и закрепления, а возможно, и событий от кнопок.
- **Редактирование старого анонса** может упереться в ошибку 909 «too old». Нужен запасной вариант — новое сообщение.
- Недокументированные количественные лимиты и капча. Нужны очередь, ограничитель частоты и повтор при ошибках 6 и 9.
- Поддержка callback-кнопок в сторонних и старых клиентах — проверять `client_info` и иметь текстовый запасной вариант.

### Что не удалось подтвердить (⚠)
1. Распространяется ли месячный лимит VK ID (10 000 / 100 млн) на ключ сообщества из настроек сообщества и учитываются ли запросы Long Poll.
2. Значение ошибки 993 «Chat moved to MAX» и условия переноса бесед в MAX; официального объявления не нашёл.
3. Приходит ли `message_event` (и `message_new` о добавлении бота) при доступе «только упоминания».
4. Формат `action.member_id` при добавлении сообщества в беседу (ожидается `-group_id`).
5. Кто может добавить бота в беседу: любой участник или только админ (вероятно, зависит от `only_admins_invite`).
6. Срок, после которого `messages.edit` возвращает 909 «too old», и условия ошибки 949 для закреплённых сообщений.
   **Проверено 2026-10-20** (бот — администратор беседы, ключ сообщества): `messages.edit` анонса возрастом ~46 ч прошёл
   (переезд беседы, кнопки при этом снялись); `messages.delete` с `delete_for_all=1` по `cmids` удалил сообщение бота
   возрастом 58,6 ч и свежее — оба ответа `response: 1`, после удаления `getByConversationMessageId` их не возвращает.
   Лимита «сутки» для бота-администратора в беседе нет. Видна ли участникам заглушка «сообщение удалено» — ⚠ не проверено.
7. Нужны ли боту права администратора для `messages.pin` (сделан вывод по ошибке 925).
8. Поддержка callback и inline-кнопок в беседах на конкретных платформах (веб, iOS, Android); матрицы в доках нет.
9. Приходит ли `poll_vote_new` за опросы в беседах; можно ли прикрепить опрос сообщества в беседу.
10. Обязателен ли HTTPS-адрес для Callback API и есть ли таймаут на ответ.
11. Лимит метода `execute` (страница не загрузилась).
12. Смысл ошибки 987 «Need message request».
13. Разрешение ЛС через кнопку `vk.me?ref=` + «Начать» — схема собрана из документированных частей, на практике не проверена.
14. Требования 152-ФЗ к обработке персональных данных — вне доков VK, юридически не проверено.

---

## Источники (официальные, загружены 18.10.2026)
- Боты, быстрый старт: https://dev.vk.com/ru/api/bots/getting-started
- Отправка сообщений ботом: https://dev.vk.com/ru/api/bots/development/messages
- Управление сообщениями (cmid, edit, pin): https://dev.vk.com/ru/api/bots/development/messages/management
- Клавиатура: https://dev.vk.com/ru/api/bots/development/keyboard
- Типы кнопок: https://dev.vk.com/ru/api/bots/development/keyboard/key-types
- Callback-кнопки: https://dev.vk.com/ru/api/bots/development/keyboard/callback-keys
- Подключение клавиатуры: https://dev.vk.com/ru/api/bots/development/keyboard/enable-keyboard
- Сообщения сообществ (разрешение ЛС, vk.me ref): https://dev.vk.com/ru/api/community-messages/getting-started
- События сообщества: https://dev.vk.com/ru/api/community-events/json-schema
- Bots Long Poll API: https://dev.vk.com/ru/api/bots-long-poll/getting-started
- Callback API: https://dev.vk.com/ru/api/callback/getting-started
- Формат запросов и лимиты: https://dev.vk.com/ru/api/api-requests
- Ключ доступа сообщества: https://dev.vk.com/ru/api/access-token/community-token
- Права доступа: https://dev.vk.com/ru/reference/access-rights
- Версии API: https://dev.vk.com/ru/reference/versions
- Ошибки: https://dev.vk.com/ru/reference/errors
- Объект сообщения: https://dev.vk.com/ru/reference/objects/message
- Методы: [messages.send](https://dev.vk.com/ru/method/messages.send), [messages.edit](https://dev.vk.com/ru/method/messages.edit), [messages.pin](https://dev.vk.com/ru/method/messages.pin), [messages.unpin](https://dev.vk.com/ru/method/messages.unpin), [messages.getConversationMembers](https://dev.vk.com/ru/method/messages.getConversationMembers), [messages.getConversationsById](https://dev.vk.com/ru/method/messages.getConversationsById), [messages.isMessagesFromGroupAllowed](https://dev.vk.com/ru/method/messages.isMessagesFromGroupAllowed), [messages.allowMessagesFromGroup](https://dev.vk.com/ru/method/messages.allowMessagesFromGroup), [messages.sendMessageEventAnswer](https://dev.vk.com/ru/method/messages.sendMessageEventAnswer), [messages.getByConversationMessageId](https://dev.vk.com/ru/method/messages.getByConversationMessageId), [groups.getLongPollServer](https://dev.vk.com/ru/method/groups.getLongPollServer), [users.get](https://dev.vk.com/ru/method/users.get), [polls.create](https://dev.vk.com/ru/method/polls.create), [polls.getVoters](https://dev.vk.com/ru/method/polls.getVoters), [polls.getById](https://dev.vk.com/ru/method/polls.getById)
- VK Bridge, разрешение сообщений: https://dev.vk.com/ru/bridge/VKWebAppAllowMessagesFromGroup
- Виджет разрешения сообщений: https://dev.vk.com/ru/widgets/allow-messages-from-community
- Правила для ботов: https://dev.vk.com/ru/bots-rules
- Правила платформы (ред. 19.08.2026): https://dev.vk.com/ru/rules
- PyPI: https://pypi.org/project/vkbottle/, https://pypi.org/project/vkbottle-types/, https://pypi.org/project/vk-api/, https://pypi.org/project/vkwave/

Сторонние (только подсказка, где искать, ничего из них не взято как факт): результаты поиска по MAX — sostav.ru, bothelp.io, smmplanner.com; по лимитам — qna.habr.com.

Техническая заметка: WebFetch к dev.vk.com не работает. curl из Windows падает на проверке отзыва сертификата (schannel `CRYPT_E_NO_REVOCATION_CHECK`), страницы скачивались с `curl --ssl-no-revoke`. Часть страниц сервер иногда отдаёт пустым SPA-шаблоном, помогает повторный запрос.
