#!/usr/bin/env bash
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Run this script as root." >&2
  exit 1
fi
if [ "$#" -ne 2 ]; then
  echo "Usage: $0 FULL_BACKUP.tar.enc DECRYPTION_KEY.txt" >&2
  exit 1
fi
if [ "${CONFIRM_RESTORE:-}" != "RESTORE" ]; then
  echo "Set CONFIRM_RESTORE=RESTORE to replace the target service data." >&2
  exit 1
fi

BACKUP_FILE="$(readlink -f "$1")"
KEY_FILE="$(readlink -f "$2")"
TARGET_DIR="${DOLA_RESTORE_TARGET:-/opt/dola-fetch-service}"
WORK_DIR="$(mktemp -d /root/dola-restore.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
  -pass "file:$KEY_FILE" -in "$BACKUP_FILE" -out "$WORK_DIR/backup.tar"
tar -tf "$WORK_DIR/backup.tar" >/dev/null
mkdir -p "$WORK_DIR/content"
tar -xf "$WORK_DIR/backup.tar" -C "$WORK_DIR/content"

test -s "$WORK_DIR/content/database/postgres.dump"
test -s "$WORK_DIR/content/redis/dump.rdb"
test -s "$WORK_DIR/content/archives/app-data.tar.zst"
test -s "$WORK_DIR/content/archives/source-repository.tar.zst"
test -s "$WORK_DIR/content/config/production.env"

mkdir -p /opt
tar --zstd -xf "$WORK_DIR/content/archives/source-repository.tar.zst" -C /opt
mkdir -p "$TARGET_DIR"
cp "$WORK_DIR/content/config/production.env" "$TARGET_DIR/.env"
cp "$WORK_DIR/content/config/compose.yaml" "$TARGET_DIR/compose.yaml"
cp "$WORK_DIR/content/config/VERSION" "$TARGET_DIR/VERSION"

cd "$TARGET_DIR"
docker compose down
docker compose up -d postgres redis

for _ in $(seq 1 60); do
  if docker compose exec -T postgres pg_isready >/dev/null 2>&1; then break; fi
  sleep 2
done

docker compose exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges' \
  < "$WORK_DIR/content/database/postgres.dump"

docker compose stop redis
REDIS_MOUNT="$(docker volume inspect -f '{{.Mountpoint}}' dola-fetch_redis-data)"
rm -f "$REDIS_MOUNT/dump.rdb"
cp "$WORK_DIR/content/redis/dump.rdb" "$REDIS_MOUNT/dump.rdb"
chown 999:999 "$REDIS_MOUNT/dump.rdb" || true

APP_MOUNT="$(docker volume inspect -f '{{.Mountpoint}}' dola-fetch_app-data)"
find "$APP_MOUNT" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
tar --zstd -xf "$WORK_DIR/content/archives/app-data.tar.zst" -C "$APP_MOUNT"

docker compose up -d
docker compose ps
echo "Restore completed. Verify /health, user counts, account counts and task counts."
