#!/bin/bash
# Installs linewatch on Amazon Linux 2023 behind nginx + gunicorn, with the
# simulator running as a second service so the live demo always has data.
# Usage: sudo bash deploy/setup.sh /path/to/linewatch-checkout
set -euo pipefail
SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
APP=/opt/linewatch

dnf install -y python3 python3-pip nginx openssl

id linewatch >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /sbin/nologin linewatch
install -d -m 755 "$APP"
rm -rf "$APP/app"
cp -r "$SRC" "$APP/app"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet -r "$APP/app/requirements.txt" gunicorn

install -d -o linewatch -g linewatch -m 750 /var/lib/linewatch

# API key is generated here and never leaves the box (not in user data,
# not in CloudFormation). Readable by root only.
if [ ! -f /etc/linewatch.env ]; then
  umask 077
  cat > /etc/linewatch.env <<EOF
LINEWATCH_API_KEY=$(openssl rand -hex 24)
LINEWATCH_DB=/var/lib/linewatch/linewatch.db
EOF
fi
chmod 600 /etc/linewatch.env

install -m 644 "$APP/app/deploy/linewatch.service" /etc/systemd/system/
install -m 644 "$APP/app/deploy/linewatch-sim.service" /etc/systemd/system/
install -m 644 "$APP/app/deploy/nginx.conf" /etc/nginx/nginx.conf
nginx -t

systemctl daemon-reload
systemctl enable --now linewatch.service
systemctl enable --now linewatch-sim.service
systemctl enable --now nginx
systemctl restart nginx
echo "linewatch installed"
