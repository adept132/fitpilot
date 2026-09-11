#!/usr/bin/env bash
set -Eeuo pipefail
set +x

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/lib/release_common.sh"
[[ $# == 0 ]] || die usage

EURITH_PUBLIC_API_URL="${EURITH_PUBLIC_API_URL:-}"
EURITH_CANARY_IDS_FILE="${EURITH_CANARY_IDS_FILE:-}"
EURITH_BASE_COMPOSE="${EURITH_BASE_COMPOSE:-/opt/eurith/docker-compose.yml}"
RELEASE_OVERLAY="${RELEASE_OVERLAY:-/opt/eurith/backend/deploy/compose.release.yml}"
DEPLOY_ENV="${DEPLOY_ENV:-/etc/eurith/release-deploy.env}"
CADDY_CONTAINER="${CADDY_CONTAINER:-caddy}"
API_CONTAINER="${API_CONTAINER:-api}"
MAX_CAPTURE_BYTES=65536
for command_name in curl docker awk grep sed stat df sha256sum base64 xxd mktemp rm; do require_command "$command_name"; done
[[ "$EURITH_PUBLIC_API_URL" =~ ^https://[^[:space:]/]+/?$ ]] || die invalid_public_api_url
base="${EURITH_PUBLIC_API_URL%/}"
[[ -f "$EURITH_CANARY_IDS_FILE" && ! -L "$EURITH_CANARY_IDS_FILE" ]] || die canary_ids_file_invalid
[[ "$(stat -c '%a:%u' "$EURITH_CANARY_IDS_FILE")" =~ ^(400|600|640):0$ ]] || die canary_ids_file_permissions_invalid

missing_release_id=''; withdrawn_release_id=''; non_direct_release_id=''; unrelated_public_path=''
while IFS='=' read -r key value; do
  [[ "$key" =~ ^(missing_release_id|withdrawn_release_id|non_direct_release_id|unrelated_public_path)$ && -n "$value" ]] || die canary_ids_file_keys_invalid
  case "$key" in
    missing_release_id) [[ -z "$missing_release_id" ]] || die canary_ids_file_keys_invalid; missing_release_id="$value" ;;
    withdrawn_release_id) [[ -z "$withdrawn_release_id" ]] || die canary_ids_file_keys_invalid; withdrawn_release_id="$value" ;;
    non_direct_release_id) [[ -z "$non_direct_release_id" ]] || die canary_ids_file_keys_invalid; non_direct_release_id="$value" ;;
    unrelated_public_path) [[ -z "$unrelated_public_path" ]] || die canary_ids_file_keys_invalid; unrelated_public_path="$value" ;;
  esac
done <"$EURITH_CANARY_IDS_FILE"
uuid='^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
public_path='^/[A-Za-z0-9_./?=&-]+$'
[[ "${missing_release_id:-}" =~ $uuid && "${withdrawn_release_id:-}" =~ $uuid && "${non_direct_release_id:-}" =~ $uuid ]] || die canary_ids_invalid
[[ "${unrelated_public_path:-}" =~ $public_path ]] || die unrelated_public_path_invalid

tmp="$(mktemp -d)"; trap 'rm -rf -- "$tmp"' EXIT
request() {
  local name="$1" method="$2" url="$3"; shift 3
  curl --silent --show-error --max-time 15 --max-filesize "$MAX_CAPTURE_BYTES" --request "$method" --dump-header "$tmp/$name.headers" --output "$tmp/$name.body" --write-out '%{http_code}' "$@" "$url"
}
expect_status() { [[ "$1" == "$2" ]] || die canary_http_status_failed; }
no_internal_header() { ! grep -Eiq '^X-Accel-Redirect:' "$1" || die internal_header_leaked; }

status="$(request health GET "$base/health")"; expect_status "$status" 200
grep -Eq '"database"[[:space:]]*:[[:space:]]*"connected"' "$tmp/health.body" || die database_health_failed
status="$(request latest GET "$base/app-releases/android/latest?channel=production-direct&current_version_code=1")"; expect_status "$status" 200
grep -Eiq '^Cache-Control:[[:space:]]*no-store' "$tmp/latest.headers" || die latest_cache_control_failed; no_internal_header "$tmp/latest.headers"

status="$(request publisher_absent GET "$base/internal/app-releases/lanes/android/production-direct")"; expect_status "$status" 401
status="$(request publisher_wrong GET "$base/internal/app-releases/lanes/android/production-direct" -H 'Authorization: Bearer invalid-canary-token')"; expect_status "$status" 401
status="$(request operator_absent PATCH "$base/internal/app-releases/$missing_release_id/mandatory" -H 'Content-Type: application/json' --data '{"mandatory":false}')"; expect_status "$status" 401
status="$(request operator_wrong PATCH "$base/internal/app-releases/$missing_release_id/mandatory" -H 'Authorization: Bearer invalid-canary-token' -H 'Content-Type: application/json' --data '{"mandatory":false}')"; expect_status "$status" 401
status="$(request webhook_wrong POST "$base/webhooks/github/mobile-push" -H 'X-Hub-Signature-256: sha256=0000000000000000000000000000000000000000000000000000000000000000' -H 'X-GitHub-Event: push' -H 'X-GitHub-Delivery: canary-invalid-signature' -H 'Content-Type: application/json' --data '{}')"; expect_status "$status" 401

status="$(request missing GET "$base/app-releases/$missing_release_id/download")"; expect_status "$status" 404; no_internal_header "$tmp/missing.headers"
status="$(request withdrawn GET "$base/app-releases/$withdrawn_release_id/download")"; expect_status "$status" 410; no_internal_header "$tmp/withdrawn.headers"
status="$(request non_direct GET "$base/app-releases/$non_direct_release_id/download")"; expect_status "$status" 404; no_internal_header "$tmp/non_direct.headers"
status="$(request internal GET "$base/_release_files/android/sha256/$(printf 0%.0s {1..64}).apk")"; expect_status "$status" 404; no_internal_header "$tmp/internal.headers"
status="$(request unrelated GET "$base$unrelated_public_path")"; [[ "$status" =~ ^(200|204|401|404)$ ]] || die unrelated_public_api_failed; no_internal_header "$tmp/unrelated.headers"

compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$RELEASE_OVERLAY")
for service in "$API_CONTAINER" "$CADDY_CONTAINER"; do
  running="$("${compose[@]}" ps --status running --services | grep -cx "$service" || true)"; [[ "$running" == 1 ]] || die container_not_running
  container_id="$("${compose[@]}" ps -q "$service")"; [[ -n "$container_id" ]] || die container_not_running
  restarts="$(docker inspect "$container_id" --format '{{.RestartCount}}' 2>/dev/null || printf invalid)"; [[ "$restarts" =~ ^[0-9]+$ && "$restarts" == 0 ]] || die unexpected_container_restart
done
runtime_logs="$("${compose[@]}" logs --since 10m --no-color "$API_CONTAINER" "$CADDY_CONTAINER" 2>&1 | tail -n 2000 | tail -c 131072)" || die runtime_log_read_failed
if grep -Eiq 'LocalProtocolError|Too little data for declared Content-Length|permission denied|release_view_gate=failed|handoff[^[:alnum:]]*502|artifact[^[:alnum:]]*503|download[^[:alnum:]]*5[0-9]{2}' <<<"$runtime_logs"; then
  die runtime_log_canary_failed
fi

available_percent="$(df -P /opt/eurith/releases | awk 'NR==2 {gsub(/%/,"",$5); print 100-$5}')"
[[ "$available_percent" =~ ^[0-9]+$ && "$available_percent" -ge 20 ]] || die release_storage_free_space_low

direct_row="$("${compose[@]}" exec -T postgres sh -c 'exec psql --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align --field-separator="|" --command "$1"' sh "SELECT id, artifact_sha256, artifact_size_bytes FROM app_releases WHERE delivery_method='direct_apk' AND status='published' AND artifact_deleted_at IS NULL ORDER BY version_code DESC, published_at DESC LIMIT 1" 2>/dev/null | sed '/^[[:space:]]*$/d')" || die release_registry_probe_failed
if [[ -z "$direct_row" ]]; then
  printf 'existing_direct_apk=not_applicable\n'
else
  IFS='|' read -r direct_id expected_sha expected_size extra <<<"$direct_row"
  [[ "$direct_id" =~ $uuid && "$expected_sha" =~ ^[0-9a-f]{64}$ && "$expected_size" =~ ^[1-9][0-9]*$ && -z "${extra:-}" ]] || die direct_release_metadata_invalid
  status="$(request direct_full GET "$base/app-releases/$direct_id/download" --max-filesize "$expected_size")"; expect_status "$status" 200; no_internal_header "$tmp/direct_full.headers"
  [[ "$(stat -c '%s' "$tmp/direct_full.body")" == "$expected_size" && "$(sha256sum "$tmp/direct_full.body" | awk '{print $1}')" == "$expected_sha" ]] || die direct_release_bytes_mismatch
  grep -Eiq "^Content-Length:[[:space:]]*$expected_size" "$tmp/direct_full.headers" || die direct_release_length_mismatch
  grep -Eiq "^ETag:[[:space:]]*\"sha256:$expected_sha\"" "$tmp/direct_full.headers" || die direct_release_etag_mismatch
  expected_digest="sha-256=$(printf '%s' "$expected_sha" | xxd -r -p | base64 | tr -d '\n')"
  grep -Fqi "Digest: $expected_digest" "$tmp/direct_full.headers" || die direct_release_digest_mismatch
  status="$(request direct_range GET "$base/app-releases/$direct_id/download" -H 'Range: bytes=0-0')"; expect_status "$status" 206
  [[ "$(stat -c '%s' "$tmp/direct_range.body")" == 1 ]] || die direct_release_range_mismatch
  grep -Eiq "^Content-Range:[[:space:]]*bytes[[:space:]]+0-0/$expected_size" "$tmp/direct_range.headers" || die direct_release_range_mismatch
  no_internal_header "$tmp/direct_range.headers"
fi
printf 'canary_result=passed\navailable_percent=%s\n' "$available_percent"
