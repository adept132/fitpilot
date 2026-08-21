#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/eurith}"
SOURCE_DIR="${SOURCE_DIR:-${APP_DIR}/backend}"
HEALTH_URL="${HEALTH_URL:-https://api.eurith.app/health}"
BACKUP_DIR="${APP_DIR}/backups"

if [[ $# -ne 1 ]]; then
  echo "Использование: $0 <commit-or-tag>" >&2
  echo "Для production всегда указывайте проверенный commit или tag явно." >&2
  exit 2
fi

REF="$1"

cd "$SOURCE_DIR"

if [[ ! -d .git ]]; then
  echo "Ошибка: $SOURCE_DIR пока не является git-клоном." >&2
  echo "Сначала выполните одноразовый перевод сервера на Git-деплой." >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Ошибка: на сервере есть незакоммиченные изменения. Деплой остановлен." >&2
  git status --short
  exit 1
fi

OLD_COMMIT="$(git rev-parse HEAD)"
mkdir -p "$BACKUP_DIR"

echo "[1/7] Загружаю изменения из GitHub"
git fetch --prune origin
NEW_COMMIT="$(git rev-parse --verify "${REF}^{commit}")"

if [[ "$NEW_COMMIT" == "$OLD_COMMIT" ]]; then
  echo "Сервер уже использует ${NEW_COMMIT}."
  exit 0
fi

echo "[2/7] Создаю резервную копию PostgreSQL"
cd "$APP_DIR"
BACKUP_FILE="${BACKUP_DIR}/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ)-${OLD_COMMIT:0:8}.dump"
docker compose exec -T postgres pg_dump \
  -U "${POSTGRES_USER:-eurith_app}" \
  -d "${POSTGRES_DB:-eurith}" \
  --format=custom --no-owner --no-privileges > "$BACKUP_FILE"
test -s "$BACKUP_FILE"

echo "[3/7] Переключаю исходники на ${NEW_COMMIT}"
cd "$SOURCE_DIR"
git checkout --detach "$NEW_COMMIT"

echo "[4/7] Собираю новый образ"
cd "$APP_DIR"
if ! docker compose build api; then
  git -C "$SOURCE_DIR" checkout --detach "$OLD_COMMIT"
  echo "Сборка не удалась; исходники возвращены на ${OLD_COMMIT}." >&2
  exit 1
fi

echo "[5/7] Применяю миграции базы данных"
if ! docker compose run --rm --no-deps api alembic upgrade head; then
  git -C "$SOURCE_DIR" checkout --detach "$OLD_COMMIT"
  echo "Миграция не удалась; работающий API не изменён." >&2
  echo "Резервная копия: $BACKUP_FILE" >&2
  exit 1
fi

echo "[6/7] Запускаю API"
docker compose up -d --no-deps api

echo "[7/7] Проверяю публичный health endpoint"
for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/dev/null; then
    echo "Деплой успешен: ${OLD_COMMIT:0:8} -> ${NEW_COMMIT:0:8}"
    echo "Резервная копия: $BACKUP_FILE"
    exit 0
  fi
  sleep 2
done

echo "Health check не прошёл. Возвращаю ${OLD_COMMIT}." >&2
git -C "$SOURCE_DIR" checkout --detach "$OLD_COMMIT"
docker compose build api
docker compose up -d --no-deps api
exit 1
