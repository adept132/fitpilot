#!/usr/bin/env bash
set -Eeuo pipefail
set +x

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/lib/release_common.sh"
HEADER_GATE="$SCRIPT_DIR/http-header-gate.py"
[[ $# == 0 ]] || die usage

EURITH_PUBLIC_API_URL="${EURITH_PUBLIC_API_URL:-}"
EURITH_CANARY_IDS_FILE="${EURITH_CANARY_IDS_FILE:-}"
EURITH_BASE_COMPOSE="${EURITH_BASE_COMPOSE:-/opt/eurith/docker-compose.yml}"
RELEASE_OVERLAY="${RELEASE_OVERLAY:-/opt/eurith/backend/deploy/compose.release.yml}"
DEPLOY_ENV="${DEPLOY_ENV:-/etc/eurith/release-deploy.env}"
CADDY_CONTAINER="${CADDY_CONTAINER:-caddy}"
API_CONTAINER="${API_CONTAINER:-api}"
MAX_CAPTURE_BYTES=65536
MAX_APK_BYTES=262144000
PUBLIC_PROBE_PATH="${EURITH_CANARY_PUBLIC_PROBE:-/openapi.json}"
for command_name in curl docker awk grep sed stat df sha256sum base64 xxd mktemp rm python3; do require_command "$command_name"; done
[[ -f "$HEADER_GATE" && ! -L "$HEADER_GATE" ]] || die header_gate_missing
base="$(python3 - "$EURITH_PUBLIC_API_URL" <<'PY'
import ipaddress, re, sys
import urllib.parse
value = sys.argv[1]
try: parsed = urllib.parse.urlsplit(value)
except ValueError: raise SystemExit(1)
if not value.isascii() or len(value) > 2048 or any(ord(char) < 33 or ord(char) == 127 for char in value):
    raise SystemExit(1)
if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or "%" in parsed.netloc or "\\" in parsed.netloc:
    raise SystemExit(1)
host = parsed.hostname
try:
    address = ipaddress.ip_address(host)
    if not address.is_global: raise SystemExit(1)
except ValueError:
    labels = host.rstrip(".").split(".")
    if len(host) > 253 or len(labels) < 2 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
        raise SystemExit(1)
try:
    port = parsed.port
    if port is not None and port == 0: raise SystemExit(1)
except ValueError: raise SystemExit(1)
print(urllib.parse.urlunsplit(("https", parsed.netloc, "", "", "")))
PY
)" || die invalid_public_api_url
case "$PUBLIC_PROBE_PATH" in /openapi.json) PUBLIC_PROBE_STATUS=200 ;; *) die public_probe_not_allowlisted ;; esac
[[ -f "$EURITH_CANARY_IDS_FILE" && ! -L "$EURITH_CANARY_IDS_FILE" ]] || die canary_ids_file_invalid
[[ "$(stat -c '%a:%u:%g' "$EURITH_CANARY_IDS_FILE")" == 400:0:0 ]] || die canary_ids_file_permissions_invalid

missing_release_id=''; withdrawn_release_id=''; non_direct_release_id=''; canary_id_lines=0
while IFS='=' read -r key value; do
  canary_id_lines=$((canary_id_lines + 1))
  [[ "$key" =~ ^(missing_release_id|withdrawn_release_id|non_direct_release_id)$ && -n "$value" ]] || die canary_ids_file_keys_invalid
  case "$key" in
    missing_release_id) [[ -z "$missing_release_id" ]] || die canary_ids_file_keys_invalid; missing_release_id="$value" ;;
    withdrawn_release_id) [[ -z "$withdrawn_release_id" ]] || die canary_ids_file_keys_invalid; withdrawn_release_id="$value" ;;
    non_direct_release_id) [[ -z "$non_direct_release_id" ]] || die canary_ids_file_keys_invalid; non_direct_release_id="$value" ;;
  esac
done <"$EURITH_CANARY_IDS_FILE"
uuid='^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
[[ "$canary_id_lines" == 3 && "$missing_release_id" =~ $uuid && "$withdrawn_release_id" =~ $uuid && "$non_direct_release_id" =~ $uuid ]] || die canary_ids_invalid

tmp="$(mktemp -d)"; trap 'rm -rf -- "$tmp"' EXIT
request() {
  local name="$1" method="$2" url="$3"; shift 3
  curl --silent --show-error --max-time 15 --max-filesize "$MAX_CAPTURE_BYTES" --request "$method" --dump-header "$tmp/$name.headers" --output "$tmp/$name.body" --write-out '%{http_code}' "$@" "$url"
}
expect_status() { [[ "$1" == "$2" ]] || die canary_http_status_failed; }
header_exact() { python3 "$HEADER_GATE" exact "$1" "$2" "$3" || die response_header_invalid; }
no_internal_header() { python3 "$HEADER_GATE" absent "$1" X-Accel-Redirect || die internal_header_leaked; }

compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$RELEASE_OVERLAY")
declare -A restart_baseline container_ids
for service in "$API_CONTAINER" "$CADDY_CONTAINER"; do
  running="$("${compose[@]}" ps --status running --services | grep -cx "$service" || true)"; [[ "$running" == 1 ]] || die container_not_running
  container_id="$("${compose[@]}" ps -q "$service")"; [[ -n "$container_id" ]] || die container_not_running
  restarts="$(docker inspect "$container_id" --format '{{.RestartCount}}' 2>/dev/null || printf invalid)"; [[ "$restarts" =~ ^[0-9]+$ ]] || die invalid_container_restart_baseline
  container_ids["$service"]="$container_id"
  restart_baseline["$service"]="$restarts"
done

status="$(request health GET "$base/health")"; expect_status "$status" 200
grep -Eq '"database"[[:space:]]*:[[:space:]]*"connected"' "$tmp/health.body" || die database_health_failed
no_internal_header "$tmp/health.headers"
status="$(request latest GET "$base/app-releases/android/latest?channel=production-direct&current_version_code=1")"; expect_status "$status" 200
header_exact "$tmp/latest.headers" Cache-Control no-store; no_internal_header "$tmp/latest.headers"

status="$(request publisher_absent GET "$base/internal/app-releases/lanes/android/production-direct")"; expect_status "$status" 401
status="$(request publisher_wrong GET "$base/internal/app-releases/lanes/android/production-direct" -H 'Authorization: Bearer invalid-canary-token')"; expect_status "$status" 401
status="$(request operator_absent PATCH "$base/internal/app-releases/$missing_release_id/mandatory" -H 'Content-Type: application/json' --data '{"mandatory":false}')"; expect_status "$status" 401
status="$(request operator_wrong PATCH "$base/internal/app-releases/$missing_release_id/mandatory" -H 'Authorization: Bearer invalid-canary-token' -H 'Content-Type: application/json' --data '{"mandatory":false}')"; expect_status "$status" 401
status="$(request webhook_wrong POST "$base/webhooks/github/mobile-push" -H 'X-Hub-Signature-256: sha256=0000000000000000000000000000000000000000000000000000000000000000' -H 'X-GitHub-Event: push' -H 'X-GitHub-Delivery: canary-invalid-signature' -H 'Content-Type: application/json' --data '{}')"; expect_status "$status" 401

status="$(request missing GET "$base/app-releases/$missing_release_id/download")"; expect_status "$status" 404; no_internal_header "$tmp/missing.headers"
status="$(request withdrawn GET "$base/app-releases/$withdrawn_release_id/download")"; expect_status "$status" 410; no_internal_header "$tmp/withdrawn.headers"
status="$(request non_direct GET "$base/app-releases/$non_direct_release_id/download")"; expect_status "$status" 404; no_internal_header "$tmp/non_direct.headers"
status="$(request internal GET "$base/_release_files/android/sha256/$(printf 0%.0s {1..64}).apk")"; expect_status "$status" 404; no_internal_header "$tmp/internal.headers"
status="$(request unrelated GET "$base$PUBLIC_PROBE_PATH")"; expect_status "$status" "$PUBLIC_PROBE_STATUS"; no_internal_header "$tmp/unrelated.headers"

runtime_log_file="$tmp/runtime.log"
"${compose[@]}" logs --since 10m --tail 2000 --no-color "$API_CONTAINER" "$CADDY_CONTAINER" >"$runtime_log_file" 2>&1 || die runtime_log_read_failed
[[ "$(stat -c '%s' "$runtime_log_file")" -le 1048576 ]] || die runtime_log_capture_too_large
if grep -Eiq 'LocalProtocolError|Too little data for declared Content-Length|permission denied|release_view_gate=failed|handoff[^[:alnum:]]*502|artifact[^[:alnum:]]*503|download[^[:alnum:]]*5[0-9]{2}' "$runtime_log_file"; then
  die runtime_log_canary_failed
fi

available_percent="$(df -P /opt/eurith/releases | awk 'NR==2 {gsub(/%/,"",$5); print 100-$5}')"
[[ "$available_percent" =~ ^[0-9]+$ && "$available_percent" -ge 20 ]] || die release_storage_free_space_low

direct_row="$("${compose[@]}" exec -T postgres sh -c 'exec psql --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align --field-separator="|" --command "$1"' sh "SELECT id, artifact_sha256, artifact_size_bytes FROM app_releases WHERE delivery_method='direct_apk' AND status='published' AND artifact_deleted_at IS NULL ORDER BY version_code DESC, published_at DESC LIMIT 1" 2>/dev/null | sed '/^[[:space:]]*$/d')" || die release_registry_probe_failed
if [[ -z "$direct_row" ]]; then
  printf 'existing_direct_apk=not_applicable\n'
else
  IFS='|' read -r direct_id expected_sha expected_size extra <<<"$direct_row"
  [[ "$direct_id" =~ $uuid && "$expected_sha" =~ ^[0-9a-f]{64}$ && "$expected_size" =~ ^[1-9][0-9]*$ && "$expected_size" -le "$MAX_APK_BYTES" && -z "${extra:-}" ]] || die direct_release_metadata_invalid
  status="$(request direct_full GET "$base/app-releases/$direct_id/download" --max-filesize "$expected_size")"; expect_status "$status" 200; no_internal_header "$tmp/direct_full.headers"
  [[ "$(stat -c '%s' "$tmp/direct_full.body")" == "$expected_size" && "$(sha256sum "$tmp/direct_full.body" | awk '{print $1}')" == "$expected_sha" ]] || die direct_release_bytes_mismatch
  header_exact "$tmp/direct_full.headers" Content-Length "$expected_size"
  header_exact "$tmp/direct_full.headers" ETag "\"sha256:$expected_sha\""
  expected_digest="sha-256=$(printf '%s' "$expected_sha" | xxd -r -p | base64 | tr -d '\n')"
  header_exact "$tmp/direct_full.headers" Digest "$expected_digest"
  status="$(request direct_range GET "$base/app-releases/$direct_id/download" -H 'Range: bytes=0-0')"; expect_status "$status" 206
  [[ "$(stat -c '%s' "$tmp/direct_range.body")" == 1 ]] || die direct_release_range_mismatch
  header_exact "$tmp/direct_range.headers" Content-Range "bytes 0-0/$expected_size"
  no_internal_header "$tmp/direct_range.headers"
fi
for service in "$API_CONTAINER" "$CADDY_CONTAINER"; do
  current_id="$("${compose[@]}" ps -q "$service")"; [[ "$current_id" == "${container_ids[$service]}" ]] || die container_recreated_during_canary
  current_restarts="$(docker inspect "$current_id" --format '{{.RestartCount}}' 2>/dev/null || printf invalid)"
  [[ "$current_restarts" == "${restart_baseline[$service]}" ]] || die container_restart_delta
done
printf 'canary_result=passed\navailable_percent=%s\n' "$available_percent"
