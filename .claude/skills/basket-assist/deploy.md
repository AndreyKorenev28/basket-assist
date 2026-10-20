# Выкладка на сервер и эксплуатация

Подробности и подводные камни — `deploy/README.md`. Сервер: VPS (Debian 13), SSH-ключ `~/.ssh/basket_assist_vps`,
адрес — одна строка `root@<IP>` в `deploy/host.local` (вне git). Ниже `$H` = `$(cat deploy/host.local)`,
`$SSH` = `ssh -i ~/.ssh/basket_assist_vps $H`.

## Выложить код
1. Тесты и линтер зелёные (см. правила в SKILL.md), изменения **закоммичены** — `deploy.sh` выкладывает только `git HEAD`
   и отказывается при незакоммиченных правках.
2. `sh deploy/deploy.sh` — архив HEAD → `/opt/basket-assist/app.new`, установка в venv, `--check` (миграции Alembic
   применяются здесь), замена `app` (предыдущая версия → `app.old`), перезапуск службы `basket-assist`.
3. Проверить: в выводе `== готово: <коммит> работает`, затем лог:
   `$SSH "journalctl -u basket-assist --since '-2 min' --no-pager | grep -E 'INFO|WARN|ERROR' | grep -vi alembic"` —
   ожидается `бот запущен`, без `ERROR`/`Traceback`.

## Выложить секреты или конфигурацию команды
Изменились `.env` или `config/tenant.toml` (например, беседа или руководители) — `sh deploy/deploy.sh --with-secrets`.
`--with-db` (затирает базу на сервере локальной) — **только при первом переезде** и только с согласия владельца.

## Логи и состояние
- Поток логов: `$SSH journalctl -u basket-assist -f`; ошибки за сутки: `... --since '-1 day' | grep -E 'ERROR|Traceback'`.
- Синхронизация пишет `[abl] синхронизация (full|hourly|...): новых матчей N, изменено N, изменений N`; ночная полная — 03:00–04:00 МСК.
- Только чтение базы: `$SSH "sqlite3 -readonly /opt/basket-assist/data/basket_assist.db '<SELECT ...>'"`. Не выводи в чат
  ФИО и VK ID без нужды.

## Откат
`$SSH "cd /opt/basket-assist && mv app app.bad && mv app.old app && systemctl restart basket-assist"`.
Если новая версия успела применить миграцию — откат кода без отката базы может не запуститься: тогда восстановить базу
из бэкапа, сделанного до выкладки.

## Бэкапы
- Автоматически: `basket-assist-backup.timer`, 04:30 МСК, `/opt/basket-assist/backups/*.db.gz`, 14 дней.
  Проверка: `$SSH "journalctl -u basket-assist-backup --since '-1 day' --no-pager | tail -3; ls -t /opt/basket-assist/backups | head -3"`.
- Вручную перед рискованной операцией: `$SSH systemctl start basket-assist-backup`.
- Копия себе: `sh deploy/fetch-backup.sh` (или «Забрать бэкап.cmd») → `data/backups/` (вне git).
- Восстановление: `systemctl stop basket-assist`, `gunzip -c backups/<файл>.db.gz > data/basket_assist.db`,
  `chown basket:basket data/basket_assist.db`, `systemctl start basket-assist`.

## Пакеты системы
Установка пакетов на сервере (`apt-get install ...`) — от root и только с согласия владельца; новый пакет добавить
и в `deploy/install.sh`, чтобы он ставился при новой установке.
