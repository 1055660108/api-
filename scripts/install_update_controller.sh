#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dola-fetch-service}"
SERVICE_PATH="/etc/systemd/system/dola-update-controller.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行" >&2
  exit 1
fi

test -d "$APP_DIR/.git"
test -f "$APP_DIR/scripts/update_controller.py"
install -d -m 0755 /run/dola-update

cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Dola Fetch Deployment Controller
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 $APP_DIR/scripts/update_controller.py
Restart=on-failure
RestartSec=5
Environment=DOLA_UPDATE_APP_DIR=$APP_DIR
Environment=DOLA_UPDATE_SOCKET=/run/dola-update/controller.sock
Environment=DOLA_UPDATE_SOCKET_GID=10001
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now dola-update-controller
for _ in $(seq 1 20); do
  test -S /run/dola-update/controller.sock && exit 0
  sleep 1
done
systemctl --no-pager --full status dola-update-controller
exit 1
