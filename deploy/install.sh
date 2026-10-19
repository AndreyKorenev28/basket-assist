#!/bin/sh
# Первичная настройка сервера (Debian 13). Запускать от root на сервере. Повторный запуск безопасен.
set -eu

echo "== пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -y -q
apt-get install -y -q python3-venv sqlite3 ufw unattended-upgrades

echo "== автоустановка обновлений безопасности"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== DNS: без IPv6 glibc ждал 5 с на каждом запросе (AAAA) — single-request-reopen"
grep -q 'single-request-reopen' /etc/resolv.conf || echo 'options single-request-reopen' >> /etc/resolv.conf

echo "== файрвол: снаружи только SSH"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

echo "== SSH: только по ключу"
cat > /etc/ssh/sshd_config.d/10-basket-assist.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t && systemctl reload ssh

echo "== пользователь и каталоги"
id basket >/dev/null 2>&1 || useradd --system --home-dir /opt/basket-assist --shell /usr/sbin/nologin basket
install -d -o root -g root -m 755 /opt/basket-assist /opt/basket-assist/app
install -d -o basket -g basket -m 700 /opt/basket-assist/data /opt/basket-assist/backups
install -d -o root -g basket -m 750 /opt/basket-assist/config

echo "== готово: дальше deploy/deploy.sh с компьютера разработчика"
