#!/bin/sh
# Выкладка с компьютера разработчика (Git Bash): код из git HEAD → сервер, venv, миграции, перезапуск.
# Использование: deploy/deploy.sh [--with-secrets] [--with-db]
#   --with-secrets  скопировать .env и config/tenant.toml (первый раз или при изменении)
#   --with-db       скопировать локальную базу data/basket_assist.db (ТОЛЬКО при первом переезде:
#                   затрёт базу на сервере; перед этим бот на сервере останавливается)
set -eu
# адрес сервера — в deploy/host.local (вне git), например: root@203.0.113.10
[ -n "${BASKET_HOST:-}" ] || BASKET_HOST=$(cat "$(dirname "$0")/host.local" 2>/dev/null || true)
HOST=${BASKET_HOST:?нет адреса сервера: создайте deploy/host.local с root@<IP> или задайте BASKET_HOST}
KEY=${BASKET_KEY:-$HOME/.ssh/basket_assist_vps}
SSH="ssh -i $KEY -o BatchMode=yes $HOST"
SCP="scp -i $KEY -o BatchMode=yes -q"
cd "$(dirname "$0")/.."

[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "Есть незакоммиченные изменения — сначала коммит"; exit 1; }
rev=$(git rev-parse --short HEAD)
echo "== код $rev → сервер"
git archive --format=tar HEAD | $SSH 'rm -rf /opt/basket-assist/app.new && mkdir /opt/basket-assist/app.new && tar -x -C /opt/basket-assist/app.new'

for arg in "$@"; do
  case "$arg" in
    --with-secrets)
      echo "== секреты и конфигурация"
      $SCP .env "$HOST:/opt/basket-assist/.env"
      $SCP config/tenant.toml "$HOST:/opt/basket-assist/config/tenant.toml"
      $SSH 'chown root:basket /opt/basket-assist/.env /opt/basket-assist/config/tenant.toml && chmod 640 /opt/basket-assist/.env /opt/basket-assist/config/tenant.toml'
      ;;
    --with-db)
      echo "== база данных (сервер: остановка бота)"
      $SSH 'systemctl stop basket-assist 2>/dev/null || true'
      $SCP data/basket_assist.db "$HOST:/opt/basket-assist/data/basket_assist.db"
      $SSH 'chown basket:basket /opt/basket-assist/data/basket_assist.db && chmod 600 /opt/basket-assist/data/basket_assist.db'
      ;;
    *) echo "неизвестный параметр: $arg"; exit 1 ;;
  esac
done

echo "== установка, миграции, перезапуск"
$SSH "set -e
cd /opt/basket-assist
rm -rf app.old && { [ -d app ] && mv app app.old || true; } && mv app.new app
echo $rev > app/REVISION
[ -x venv/bin/python ] || python3 -m venv venv
venv/bin/python -m pip install -q --upgrade pip
venv/bin/python -m pip install -q -e app
install -m 644 app/deploy/basket-assist.service app/deploy/basket-assist-backup.service app/deploy/basket-assist-backup.timer /etc/systemd/system/
chmod 755 app/deploy/backup.sh
systemctl daemon-reload
systemctl enable -q basket-assist basket-assist-backup.timer
systemctl start basket-assist-backup.timer
# миграции и загрузка конфигурации — от пользователя сервиса, затем перезапуск
runuser -u basket -- env DB_PATH=/opt/basket-assist/data/basket_assist.db TENANT_CONFIG=/opt/basket-assist/config/tenant.toml \
  PYTHONIOENCODING=utf-8 venv/bin/python -m basket_assist --check
systemctl restart basket-assist
sleep 3
systemctl is-active basket-assist"
echo "== готово: $rev работает. Логи: ssh … journalctl -u basket-assist -f"
