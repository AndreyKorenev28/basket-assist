#!/bin/sh
# Бэкап SQLite без остановки бота (sqlite3 .backup — согласованная копия), gzip, хранение 14 дней.
set -eu
DB=/opt/basket-assist/data/basket_assist.db
DIR=/opt/basket-assist/backups
KEEP_DAYS=14

stamp=$(date +%Y-%m-%d_%H%M)
tmp="$DIR/.basket_assist_$stamp.db"
sqlite3 "$DB" ".backup '$tmp'"
sqlite3 "$tmp" "PRAGMA integrity_check;" | grep -qx ok
gzip -9 "$tmp"
mv "$tmp.gz" "$DIR/basket_assist_$stamp.db.gz"
find "$DIR" -name 'basket_assist_*.db.gz' -mtime +"$KEEP_DAYS" -delete
echo "бэкап: $DIR/basket_assist_$stamp.db.gz"
