#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_PATH="/etc/nginx/conf.d/dola-resource-limits.conf"
BACKUP_PATH="$(mktemp)"
HAD_CONFIG=false

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "请使用 root 运行此脚本" >&2
  exit 1
fi
if ! command -v nginx >/dev/null 2>&1; then
  echo "Nginx 尚未安装" >&2
  exit 1
fi

if [[ -f "$CONFIG_PATH" ]]; then
  cp "$CONFIG_PATH" "$BACKUP_PATH"
  HAD_CONFIG=true
fi

cat >"$CONFIG_PATH" <<'EOF'
limit_conn_zone $binary_remote_addr zone=dola_per_ip:10m;
limit_req_zone $binary_remote_addr zone=dola_requests:10m rate=60r/s;

limit_conn dola_per_ip 80;
limit_conn_status 429;
limit_req zone=dola_requests burst=180 nodelay;
limit_req_status 429;

client_header_timeout 15s;
client_body_timeout 120s;
send_timeout 600s;
keepalive_timeout 30s;
keepalive_requests 500;
reset_timedout_connection on;
EOF

if nginx -t; then
  systemctl reload nginx
  rm -f "$BACKUP_PATH"
  echo "Nginx 异常连接限制已生效"
  exit 0
fi

if [[ "$HAD_CONFIG" == true ]]; then
  cp "$BACKUP_PATH" "$CONFIG_PATH"
else
  rm -f "$CONFIG_PATH"
fi
rm -f "$BACKUP_PATH"
nginx -t
echo "Nginx 配置检查失败，已恢复原配置" >&2
exit 1
