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
