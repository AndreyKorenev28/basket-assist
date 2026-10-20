# Развёртывание на VPS

Сервер: VPS в РФ, Debian 13 (Python 3.13 из коробки), доступ — SSH по ключу `~/.ssh/basket_assist_vps`.
Адрес сервера — в `deploy/host.local` (вне git, одна строка `root@<IP>`) или переменной `BASKET_HOST`; ключ — `BASKET_KEY` (по умолчанию `~/.ssh/basket_assist_vps`).

## Раскладка на сервере
```
/opt/basket-assist/
├── app/          код (git HEAD), заменяется при каждой выкладке; app.old — предыдущая версия
├── venv/         Python-окружение (пакет установлен как -e app)
├── .env          секреты (VK_GROUP_TOKEN, VK_GROUP_ID) — root:basket 640
├── config/tenant.toml   команда, беседа, капитан — root:basket 640
├── data/basket_assist.db   база — basket 600
└── backups/      ежедневные бэкапы *.db.gz, 14 дней — basket 700
```
Бот работает от пользователя `basket` (без shell и прав root), служба `basket-assist` (systemd, автоперезапуск,
писать может только в `data/`). Бэкап — `basket-assist-backup.timer`, 04:30 МСК.

## Первый раз
1. На сервере от root: `sh install.sh` (скопировать: `scp -i ~/.ssh/basket_assist_vps deploy/install.sh root@<IP>:/root/`).
   Ставит пакеты и автообновления безопасности, чинит DNS (без IPv6 запросы ждали 5 с), включает файрвол (только SSH),
   запрещает вход по паролю, создаёт пользователя и каталоги.
2. С компьютера (Git Bash, из корня проекта): `deploy/deploy.sh --with-secrets --with-db`.
   **Перед этим остановить бота на компьютере** — два бота на одном сообществе делят события Long Poll.

## Обновление
Закоммитить изменения → `deploy/deploy.sh`. Скрипт отказывается выкладывать незакоммиченное.
Миграции применяются при старте (`--check` перед перезапуском). Откат: на сервере `mv app app.bad && mv app.old app && systemctl restart basket-assist`.

## Эксплуатация
- Логи: `ssh -i ~/.ssh/basket_assist_vps root@<IP> journalctl -u basket-assist -f` (без текста переписки и ПДн сверх ID).
- Статус: `systemctl status basket-assist`; память ограничена 400 МБ.
- Синхронизация вручную: `runuser -u basket -- env DB_PATH=… TENANT_CONFIG=… venv/bin/python -m basket_assist --sync --show` в `/opt/basket-assist`.
- Бэкап вне сервера: `deploy/fetch-backup.sh` → `data/backups/` (вне git). Делать хотя бы раз в неделю.
- Восстановление: остановить службу, `gunzip -c backups/<файл>.db.gz > data/basket_assist.db`, `chown basket:basket`, запустить.

## Подводные камни (встретились на RUVDS, Debian 13, 2026-10-19)
- **Вход по ключу.** Ключ, указанный при заказе, не добавился — добавляли вручную через SSH с паролем root
  (`~/.ssh/authorized_keys`). После `install.sh` вход по паролю закрыт; на крайний случай — консоль в панели провайдера.
- **GRUB.** `apt upgrade` падал на `grub-pc`: в шаблоне образа прописан несуществующий диск. Лечение (сервер BIOS,
  один диск `sda`): `echo "grub-pc grub-pc/install_devices multiselect /dev/disk/by-id/<диск sda>" | debconf-set-selections`
  и `dpkg --configure -a`. ID диска — `ls -l /dev/disk/by-id/`.
- **DNS.** Без IPv6 каждый запрос ждал 5 с (glibc ждёт ответ на AAAA). `install.sh` добавляет `options single-request-reopen`
  в `/etc/resolv.conf`. Проверка: `curl -w "%{time_namelookup}\n" -o /dev/null -s https://api.vk.com` — должно быть < 0,5 с.
- **CRLF.** На Windows `git archive` при `core.autocrlf=true` отдавал файлы с CRLF — на сервере `#!/bin/sh\r` не запускался
  (бэкап падал с 203/EXEC). Исправлено `.gitattributes` (`eol=lf`); новые скрипты для сервера — только с LF.
- **Два бота.** Бот на компьютере разработчика больше не запускать: два процесса на одном сообществе делят события Long Poll.
- **Шрифт с кириллицей.** В образе Debian нет ни одного TTF-шрифта — список «игроки» уходил текстом вместо картинки
  (в логе `нет шрифта с кириллицей`). `install.sh` ставит `fonts-dejavu-core`; на уже настроенном сервере:
  `apt-get install -y fonts-dejavu-core` (от root) и `systemctl restart basket-assist` — результат поиска шрифта кэшируется до перезапуска.
