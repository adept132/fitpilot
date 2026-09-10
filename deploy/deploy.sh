#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/eurith}"
SOURCE_DIR="${SOURCE_DIR:-${APP_DIR}/backend}"
HEALTH_URL="${HEALTH_URL:-https://api.eurith.app/health}"
BACKUP_DIR="${APP_DIR}/backups"

if [[ $# -ne 1 ]]; then
  echo "Использование: $0 <full-40-hex-commit-sha>" >&2
  echo "Для production всегда указывайте полный проверенный SHA commit явно." >&2
  exit 2
fi

TARGET_SHA="$1"
if ! [[ "$TARGET_SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "Ошибка: требуется полный 40-символьный hexadecimal SHA commit." >&2
  exit 2
fi
TARGET_SHA="${TARGET_SHA,,}"

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

print_backup_evidence() {
  echo "Backup: $BACKUP_FILE" >&2
  echo "Backup SHA-256: $BACKUP_SHA256" >&2
}

restore_source_before_api() {
  local reason="$1"
  if ! git -C "$SOURCE_DIR" checkout --detach "$OLD_COMMIT"; then
    echo "$reason; failed to restore source checkout to ${OLD_COMMIT}." >&2
  else
    echo "$reason; running API was not restarted." >&2
  fi
  print_backup_evidence
  exit 1
}

rollback_api() {
  local reason="$1"
  echo "$reason; restoring ${OLD_COMMIT}." >&2
  print_backup_evidence
  if ! git -C "$SOURCE_DIR" checkout --detach "$OLD_COMMIT"; then
    echo "Rollback failed: could not restore source checkout." >&2
    return 1
  fi
  if ! docker compose build api; then
    echo "Rollback failed: could not rebuild old API image." >&2
    return 1
  fi
  if ! docker compose up -d --no-deps api; then
    echo "Rollback failed: could not restart old API." >&2
    return 1
  fi
  echo "Old API restored." >&2
}

echo "[1/8] Загружаю изменения из GitHub"
git fetch --prune origin
NEW_COMMIT="$(git rev-parse --verify "${TARGET_SHA}^{commit}")"
if [[ "$NEW_COMMIT" != "$TARGET_SHA" ]]; then
  echo "Ошибка: SHA не разрешился ровно в запрошенный commit." >&2
  exit 1
fi

if [[ "$NEW_COMMIT" == "$OLD_COMMIT" ]]; then
  echo "Сервер уже использует ${NEW_COMMIT}."
  exit 0
fi

echo "[2/8] Создаю резервную копию PostgreSQL"
cd "$APP_DIR"
BACKUP_FILE="${BACKUP_DIR}/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ)-${OLD_COMMIT:0:8}.dump"
if ! docker compose exec -T postgres pg_dump \
  -U "${POSTGRES_USER:-eurith_app}" \
  -d "${POSTGRES_DB:-eurith}" \
  --format=custom --no-owner --no-privileges > "$BACKUP_FILE"; then
  echo "Не удалось создать резервную копию: $BACKUP_FILE" >&2
  exit 1
fi
if ! test -s "$BACKUP_FILE"; then
  echo "Резервная копия пуста: $BACKUP_FILE" >&2
  exit 1
fi
BACKUP_SHA256="$(sha256sum "$BACKUP_FILE" | awk '{print $1}')"
if [[ -z "$BACKUP_SHA256" ]]; then
  echo "Не удалось вычислить SHA-256 резервной копии: $BACKUP_FILE" >&2
  exit 1
fi
if ! pg_restore --list "$BACKUP_FILE" >/dev/null; then
  echo "Резервная копия не прошла проверку pg_restore." >&2
  print_backup_evidence
  exit 1
fi
echo "Резервная копия: $BACKUP_FILE"
echo "SHA-256 резервной копии: $BACKUP_SHA256"

echo "[3/8] Переключаю исходники на ${NEW_COMMIT}"
cd "$SOURCE_DIR"
if ! git checkout --detach "$NEW_COMMIT"; then
  echo "Не удалось переключить исходники на target commit." >&2
  print_backup_evidence
  exit 1
fi

echo "[4/8] Собираю новый образ"
cd "$APP_DIR"
if ! docker compose build api; then
  restore_source_before_api "Сборка не удалась"
fi

echo "[5/8] Применяю миграции базы данных"
if ! docker compose run --rm --no-deps api alembic upgrade head; then
  restore_source_before_api "Миграция не удалась"
fi

echo "[6/8] Применяю и проверяю системный каталог локализаций"
if ! docker compose run --rm --no-deps api python scripts/localization_catalog_gate.py; then
  restore_source_before_api "Проверка каталога не прошла"
fi

echo "[7/8] Запускаю API"
if ! docker compose up -d --no-deps api; then
  if ! rollback_api "API failed to start"; then
    exit 1
  fi
  exit 1
fi

echo "[8/8] Проверяю публичный health endpoint"
for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/dev/null; then
    echo "Деплой успешен: ${OLD_COMMIT:0:8} -> ${NEW_COMMIT:0:8}"
    echo "Резервная копия: $BACKUP_FILE"
    echo "SHA-256 резервной копии: $BACKUP_SHA256"
    exit 0
  fi
  sleep 2
done

if ! rollback_api "Health check failed"; then
  exit 1
fi
exit 1
