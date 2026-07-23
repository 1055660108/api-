#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="dola-fetch-service"
DOMAIN="${1:-huisull.xyz}"
EMAIL="${2:-}"
EXPECTED_IP="${DOLA_PUBLIC_IP:-186.241.120.51}"
UPSTREAM="http://127.0.0.1:${DOLA_PORT:-8088}"
NGINX_AVAILABLE="/etc/nginx/sites-available/$APP_NAME"
NGINX_ENABLED="/etc/nginx/sites-enabled/$APP_NAME"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "请使用 root 运行此脚本" >&2
  exit 1
fi

if [[ ! "$DOMAIN" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]; then
  echo "域名格式无效：$DOMAIN" >&2
  exit 1
fi

if [[ -z "$EMAIL" || "$EMAIL" != *@*.* ]]; then
  echo "请提供用于 Let's Encrypt 到期通知的邮箱" >&2
  echo "用法：bash scripts/install_public_https.sh $DOMAIN you@example.com" >&2
  exit 1
fi

mapfile -t DOMAIN_IPV4 < <(getent ahostsv4 "$DOMAIN" | awk '{print $1}' | sort -u)
if [[ "${#DOMAIN_IPV4[@]}" -eq 0 ]]; then
  echo "$DOMAIN 尚未解析。请先添加 A 记录：@ -> $EXPECTED_IP" >&2
  exit 1
fi
DOMAIN_POINTS_HERE=false
for ip in "${DOMAIN_IPV4[@]}"; do
  if [[ "$ip" == "$EXPECTED_IP" ]]; then
    DOMAIN_POINTS_HERE=true
    break
  fi
done
if [[ "$DOMAIN_POINTS_HERE" != true ]]; then
  echo "$DOMAIN 当前未指向本服务器 $EXPECTED_IP，检测到：${DOMAIN_IPV4[*]}" >&2
  exit 1
fi

echo "$DOMAIN 当前解析到：${DOMAIN_IPV4[*]}"
echo "即将安装 Nginx 并为 $DOMAIN 申请 HTTPS 证书。"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot python3-certbot-nginx

install -d -m 0755 /etc/nginx/sites-available /etc/nginx/sites-enabled
cat >"$NGINX_AVAILABLE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    server_tokens off;
    client_max_body_size 64m;

    location = / {
        return 302 /client;
    }

    location / {
        proxy_pass $UPSTREAM;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 10s;
        proxy_send_timeout 600s;
        proxy_read_timeout 600s;
        proxy_buffering off;
    }
}
EOF

ln -sfn "$NGINX_AVAILABLE" "$NGINX_ENABLED"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow 'Nginx Full'
fi

certbot --nginx \
  -d "$DOMAIN" \
  --email "$EMAIL" \
  --agree-tos \
  --non-interactive \
  --redirect

nginx -t
systemctl reload nginx
systemctl enable --now certbot.timer >/dev/null 2>&1 || true

echo "HTTPS 已启用：https://$DOMAIN/client"
curl --fail --show-error --silent --max-time 15 "https://$DOMAIN/health/live"
echo
