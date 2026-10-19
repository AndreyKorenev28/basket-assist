#!/bin/sh
# Забрать свежий бэкап базы с сервера на компьютер (копия вне сервера). Git Bash.
set -eu
# адрес сервера — в deploy/host.local (вне git), например: root@203.0.113.10
[ -n "${BASKET_HOST:-}" ] || BASKET_HOST=$(cat "$(dirname "$0")/host.local" 2>/dev/null || true)
HOST=${BASKET_HOST:?нет адреса сервера: создайте deploy/host.local с root@<IP> или задайте BASKET_HOST}
KEY=${BASKET_KEY:-$HOME/.ssh/basket_assist_vps}
cd "$(dirname "$0")/.."
mkdir -p data/backups
latest=$(ssh -i "$KEY" -o BatchMode=yes "$HOST" 'ls -1t /opt/basket-assist/backups/basket_assist_*.db.gz 2>/dev/null | head -1')
[ -n "$latest" ] || { echo "на сервере ещё нет бэкапов"; exit 1; }
scp -i "$KEY" -o BatchMode=yes -q "$HOST:$latest" data/backups/
echo "скачан: data/backups/$(basename "$latest")"
