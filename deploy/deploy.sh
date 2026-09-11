#!/usr/bin/env bash
set -Eeuo pipefail
set +x

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RUNNER_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
source "$SCRIPT_DIR/lib/release_common.sh"

[[ $# == 2 ]] || die usage
TARGET_SHA="${1,,}"; MOBILE_CANDIDATE_SHA="${2,,}"
ROLLBACK_SHA="${EURITH_ROLLBACK_SHA:-}"; ROLLBACK_SHA="${ROLLBACK_SHA,,}"
require_full_sha "$TARGET_SHA"
require_full_sha "$MOBILE_CANDIDATE_SHA"
require_full_sha "$ROLLBACK_SHA"

APP_DIR="${APP_DIR:-/opt/eurith}"
SOURCE_DIR="${SOURCE_DIR:-$APP_DIR/backend}"
EURITH_DEPLOY_ASSET_ROOT="${EURITH_DEPLOY_ASSET_ROOT:-}"
EURITH_BASE_COMPOSE="${EURITH_BASE_COMPOSE:-$APP_DIR/docker-compose.yml}"
RELEASE_OVERLAY="${RELEASE_OVERLAY:-$EURITH_DEPLOY_ASSET_ROOT/deploy/compose.release.yml}"
DEPLOY_ENV="${DEPLOY_ENV:-/etc/eurith/release-deploy.env}"
EURITH_PUBLIC_API_URL="${EURITH_PUBLIC_API_URL:-}"
EURITH_CANARY_IDS_FILE="${EURITH_CANARY_IDS_FILE:-/etc/eurith/release-canary-ids.env}"
EURITH_BACKUP_ROOT="${EURITH_BACKUP_ROOT:-$APP_DIR/backups/releases}"
EURITH_RESTORE_DB_URL_FILE="${EURITH_RESTORE_DB_URL_FILE:-}"
EURITH_RESTORE_VOLUME_ROOT="${EURITH_RESTORE_VOLUME_ROOT:-}"
EURITH_MIGRATION_APPROVAL_FILE="${EURITH_MIGRATION_APPROVAL_FILE:-}"
EURITH_EVIDENCE_ROOT="${EURITH_EVIDENCE_ROOT:-$APP_DIR/deploy-evidence}"
EURITH_RELEASE_VOLUME_ROOT="${EURITH_RELEASE_VOLUME_ROOT:-$APP_DIR/releases}"
EURITH_SECRET_SOURCE_DIR="${EURITH_SECRET_SOURCE_DIR:-/etc/eurith/release-secret-source}"
APPROVED_REMOTE_REF="${APPROVED_REMOTE_REF:-origin/main}"
CADDY_IMAGE="caddy:2.11.4"
EURITH_APPROVED_CADDY_DIGEST="${EURITH_APPROVED_CADDY_DIGEST:-}"
CADDY_IMAGE_REF="$CADDY_IMAGE@$EURITH_APPROVED_CADDY_DIGEST"
export CADDY_IMAGE_REF
CADDYFILE="$EURITH_DEPLOY_ASSET_ROOT/deploy/caddy/Caddyfile"
MOBILE_GATE_PUBLISHER="$RUNNER_ROOT/deploy/publish-mobile-gate.py"
MIGRATION_MANIFEST_TOOL="$RUNNER_ROOT/deploy/migration-path-manifest.py"
MIGRATION_APPROVAL_TOOL="$RUNNER_ROOT/deploy/validate-migration-approval.py"
REHEARSAL_PROBE="$RUNNER_ROOT/deploy/rehearse-release-db.py"
REHEARSAL_INPUT_PREPARER="$RUNNER_ROOT/deploy/prepare-rehearsal-inputs.py"
MOBILE_GATE_FILE="${MOBILE_GATE_FILE:-$EURITH_EVIDENCE_ROOT/backend-gate-${TARGET_SHA}.env}"

for command_name in git docker curl sha256sum awk sed grep stat findmnt head python3 install chmod date mktemp mv rm rmdir seq sleep; do require_command "$command_name"; done
for path in "$SOURCE_DIR" "$EURITH_DEPLOY_ASSET_ROOT" "$EURITH_BASE_COMPOSE" "$RELEASE_OVERLAY" "$DEPLOY_ENV" "$EURITH_BACKUP_ROOT" "$EURITH_RESTORE_DB_URL_FILE" "$EURITH_RESTORE_VOLUME_ROOT" "$EURITH_MIGRATION_APPROVAL_FILE" "$EURITH_EVIDENCE_ROOT" "$EURITH_CANARY_IDS_FILE" "$MOBILE_GATE_FILE"; do [[ -n "$path" ]] || die required_runtime_path_missing; done
PUBLIC_API_BASE="$(python3 - "$EURITH_PUBLIC_API_URL" <<'PY'
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
[[ "$EURITH_APPROVED_CADDY_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die missing_caddy_digest
[[ -f "$DEPLOY_ENV" && ! -L "$DEPLOY_ENV" ]] || die protected_deploy_env_missing
[[ "$(stat -c '%a:%u' "$DEPLOY_ENV")" =~ ^(600|640):0$ ]] || die protected_deploy_env_permissions_invalid
[[ -f "$EURITH_BASE_COMPOSE" && ! -L "$EURITH_BASE_COMPOSE" ]] || die base_compose_invalid
[[ -f "$RELEASE_OVERLAY" && ! -L "$RELEASE_OVERLAY" ]] || die release_overlay_invalid
[[ -f "$MOBILE_GATE_PUBLISHER" && ! -L "$MOBILE_GATE_PUBLISHER" ]] || die mobile_gate_publisher_invalid
for helper in "$MIGRATION_MANIFEST_TOOL" "$MIGRATION_APPROVAL_TOOL" "$REHEARSAL_PROBE" "$REHEARSAL_INPUT_PREPARER"; do [[ -f "$helper" && ! -L "$helper" ]] || die migration_release_helper_invalid; done
[[ -f "$EURITH_CANARY_IDS_FILE" && ! -L "$EURITH_CANARY_IDS_FILE" ]] || die canary_ids_file_invalid
[[ -f "$EURITH_MIGRATION_APPROVAL_FILE" && ! -L "$EURITH_MIGRATION_APPROVAL_FILE" ]] || die migration_approval_file_invalid
[[ -f "$EURITH_RESTORE_DB_URL_FILE" && ! -L "$EURITH_RESTORE_DB_URL_FILE" ]] || die restore_database_url_file_invalid

require_safe_absolute_path "$EURITH_BACKUP_ROOT" backup "$SOURCE_DIR"
require_safe_absolute_path "$EURITH_EVIDENCE_ROOT" backup "$SOURCE_DIR"
require_safe_absolute_path "$EURITH_RESTORE_VOLUME_ROOT" backup "$SOURCE_DIR"
require_safe_absolute_path "$EURITH_MIGRATION_APPROVAL_FILE" secret "$SOURCE_DIR"
require_safe_absolute_path "$EURITH_RESTORE_DB_URL_FILE" secret "$SOURCE_DIR"
require_safe_absolute_path "$MOBILE_GATE_FILE" backup "$SOURCE_DIR"
require_safe_absolute_path "$EURITH_DEPLOY_ASSET_ROOT" backup "$SOURCE_DIR"
[[ "$(_canonical_path "$EURITH_DEPLOY_ASSET_ROOT")" == "$(_canonical_path "$RUNNER_ROOT")" ]] || die deploy_asset_root_mismatch
[[ "$(_canonical_path "$RELEASE_OVERLAY")" == "$(_canonical_path "$EURITH_DEPLOY_ASSET_ROOT")/deploy/compose.release.yml" ]] || die deploy_asset_overlay_mismatch
case "$(_canonical_path "$EURITH_EVIDENCE_ROOT")/" in "$(_canonical_path "$EURITH_RELEASE_VOLUME_ROOT")"/*) die evidence_below_release_storage ;; esac
export EURITH_RUNTIME_SOURCE_ROOT="$EURITH_DEPLOY_ASSET_ROOT"
export EURITH_RUNTIME_ASSET_ROOT="$EURITH_DEPLOY_ASSET_ROOT"
compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$RELEASE_OVERLAY")
OLD_COMMIT=''; EVIDENCE_DIR=''; BACKUP_GENERATION=''; BACKUP_MANIFEST_SHA256=''; EXPECTED_ALEMBIC_HEAD=''; MIGRATION_ATTEMPTED=0; MIGRATION_STATE=not_attempted; MIGRATION_EVIDENCE_WRITTEN=0; SWITCH_ATTEMPTED=0; SOURCE_SWITCHED=0; SCHEMA_ROLLBACK_COMPATIBLE=0; ROLLBACK_ATTEMPTED=0; ROLLBACK_EVIDENCE_WRITTEN=0; PRIOR_CADDY_PRESENT=0; OLD_CADDY_IMAGE_ID=''; REHEARSAL_DIR=''; REHEARSAL_DB_URL_SNAPSHOT=''; REHEARSAL_OVERLAY=''; CURRENT_STAGE=preflight
declare -A SWITCH_CONTAINER_IDS=()
evidence() {
  local key="$1" value="$2"
  [[ "$key" =~ ^[a-z_]+$ && "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *'='* ]] || die invalid_evidence
  printf '%s=%s\n' "$key" "$value" >>"$EVIDENCE_DIR/deploy.env"
}

gate_checkout() {
  cd "$SOURCE_DIR"; [[ -d .git ]] || die checkout_missing
  [[ -z "$(git status --porcelain)" ]] || die checkout_dirty
  require_root
  OLD_COMMIT="$(git rev-parse HEAD)"; require_full_sha "${OLD_COMMIT,,}"
  [[ -d "$EURITH_DEPLOY_ASSET_ROOT/.git" || -f "$EURITH_DEPLOY_ASSET_ROOT/.git" ]] || die deploy_runner_not_versioned
  [[ "$(git -C "$EURITH_DEPLOY_ASSET_ROOT" rev-parse --show-toplevel)" == "$(_canonical_path "$EURITH_DEPLOY_ASSET_ROOT")" ]] || die deploy_asset_root_toplevel_mismatch
  [[ -z "$(git -C "$EURITH_DEPLOY_ASSET_ROOT" status --porcelain)" ]] || die deploy_asset_root_dirty
  [[ "$(git -C "$EURITH_DEPLOY_ASSET_ROOT" rev-parse HEAD)" == "$TARGET_SHA" ]] || die deploy_asset_root_sha_mismatch
  if git -C "$EURITH_DEPLOY_ASSET_ROOT" symbolic-ref -q HEAD >/dev/null 2>&1; then die deploy_asset_root_not_detached; fi
  git fetch --prune origin >/dev/null || die fetch_failed
  [[ "$(git rev-parse --verify "${TARGET_SHA}^{commit}")" == "$TARGET_SHA" ]] || die target_sha_unresolved
  [[ "$(git rev-parse --verify "${ROLLBACK_SHA}^{commit}")" == "$ROLLBACK_SHA" ]] || die rollback_sha_unresolved
  git rev-parse --verify "${APPROVED_REMOTE_REF}^{commit}" >/dev/null || die approved_remote_ref_missing
  git merge-base --is-ancestor "$TARGET_SHA" "$APPROVED_REMOTE_REF" || die target_not_in_approved_remote_ref
  git merge-base --is-ancestor "$ROLLBACK_SHA" "$TARGET_SHA" || die rollback_not_ancestor_of_target
  git merge-base --is-ancestor "$ROLLBACK_SHA" "$APPROVED_REMOTE_REF" || die rollback_not_in_approved_remote_ref
  if [[ ! -e "$EURITH_EVIDENCE_ROOT" ]]; then install -d -o 0 -g 0 -m 0700 -- "$EURITH_EVIDENCE_ROOT"; fi
  assert_mode_owner_group "$EURITH_EVIDENCE_ROOT" 700 0 0
  [[ "$(_canonical_path "$(dirname -- "$MOBILE_GATE_FILE")")" == "$(_canonical_path "$EURITH_EVIDENCE_ROOT")" ]] || die mobile_gate_parent_invalid
  [[ ! -e "$MOBILE_GATE_FILE" && ! -L "$MOBILE_GATE_FILE" ]] || die mobile_gate_already_exists
  EVIDENCE_DIR="$EURITH_EVIDENCE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$TARGET_SHA"
  [[ ! -e "$EVIDENCE_DIR" && ! -L "$EVIDENCE_DIR" ]] || die evidence_generation_exists
  install -d -m 0700 -- "$EVIDENCE_DIR"; : >"$EVIDENCE_DIR/deploy.env"; chmod 0600 "$EVIDENCE_DIR/deploy.env"
  evidence old_backend_sha "$OLD_COMMIT"; evidence rollback_backend_sha "$ROLLBACK_SHA"; evidence new_backend_sha "$TARGET_SHA"; evidence mobile_candidate_sha "$MOBILE_CANDIDATE_SHA"
}

gate_pause() {
  [[ "${RELEASE_MUTATIONS_PAUSED:-}" == 1 ]] || die release_mutations_not_paused
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet eurith-release-cleanup.timer; then die cleanup_timer_still_active; fi
  evidence mutations_paused passed
}

snapshot_rehearsal_inputs() {
  local output
  output="$(python3 "$REHEARSAL_INPUT_PREPARER" "$EURITH_RESTORE_DB_URL_FILE" "$REHEARSAL_PROBE")" || die rehearsal_input_snapshot_failed
  REHEARSAL_DIR="$(sed -n 's/^rehearsal_dir=//p' <<<"$output")"
  REHEARSAL_DB_URL_SNAPSHOT="$(sed -n 's/^rehearsal_database_url_snapshot=//p' <<<"$output")"
  REHEARSAL_OVERLAY="$(sed -n 's/^rehearsal_overlay=//p' <<<"$output")"
  [[ "$REHEARSAL_DIR" =~ ^/tmp/eurith-release-rehearsal\.[A-Za-z0-9_-]+$ ]] || die rehearsal_snapshot_output_invalid
  [[ "$REHEARSAL_DB_URL_SNAPSHOT" == "$REHEARSAL_DIR/restore-db.env" && "$REHEARSAL_OVERLAY" == "$REHEARSAL_DIR/compose.rehearsal.yml" ]] || die rehearsal_snapshot_output_invalid
  [[ -f "$REHEARSAL_DB_URL_SNAPSHOT" && ! -L "$REHEARSAL_DB_URL_SNAPSHOT" && "$(stat -c '%a:%u:%g' "$REHEARSAL_DB_URL_SNAPSHOT")" == 400:0:0 ]] || die rehearsal_database_snapshot_invalid
  [[ -f "$REHEARSAL_OVERLAY" && ! -L "$REHEARSAL_OVERLAY" && "$(stat -c '%a:%u:%g' "$REHEARSAL_OVERLAY")" == 400:0:0 ]] || die rehearsal_overlay_invalid
  evidence rehearsal_input_snapshot passed
}

cleanup_rehearsal_inputs() {
  [[ -n "$REHEARSAL_DIR" ]] || return 0
  [[ "$REHEARSAL_DIR" =~ ^/tmp/eurith-release-rehearsal\.[A-Za-z0-9_-]+$ ]] || return 1
  [[ "$REHEARSAL_DB_URL_SNAPSHOT" == "$REHEARSAL_DIR/restore-db.env" && "$REHEARSAL_OVERLAY" == "$REHEARSAL_DIR/compose.rehearsal.yml" ]] || return 1
  rm -f -- "$REHEARSAL_OVERLAY" "$REHEARSAL_DB_URL_SNAPSHOT" || return 1
  rmdir -- "$REHEARSAL_DIR"
  REHEARSAL_DIR=''; REHEARSAL_DB_URL_SNAPSHOT=''; REHEARSAL_OVERLAY=''
}

create_paired_backup() {
  local output manifest_hash
  output="$(RELEASE_MUTATIONS_PAUSED=1 RELEASE_CHECKOUT_ROOT="$SOURCE_DIR" "$SCRIPT_DIR/backup-release-state.sh" "$OLD_COMMIT" "$EURITH_BACKUP_ROOT")" || die paired_backup_failed
  BACKUP_GENERATION="$(sed -n 's/^generation=//p' <<<"$output")"; manifest_hash="$(sed -n 's/^manifest_sha256=//p' <<<"$output")"
  [[ "$BACKUP_GENERATION" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{40}$ && "$manifest_hash" =~ ^[0-9a-f]{64}$ ]] || die backup_manifest_invalid
  BACKUP_MANIFEST_SHA256="$manifest_hash"; evidence backup_manifest_sha256 "$manifest_hash"
}

verify_isolated_restore() {
  local output restored_manifest
  output="$("$SCRIPT_DIR/verify-release-restore.sh" "$EURITH_BACKUP_ROOT/$BACKUP_GENERATION" "$REHEARSAL_DB_URL_SNAPSHOT" "$EURITH_RESTORE_VOLUME_ROOT")" || die isolated_restore_failed
  restored_manifest="$(sed -n 's/^manifest_sha256=//p' <<<"$output")"
  [[ "$restored_manifest" =~ ^[0-9a-f]{64}$ && "$restored_manifest" == "$BACKUP_MANIFEST_SHA256" ]] || die backup_restore_manifest_mismatch
  evidence restore_drill passed
}

checkout_target_source() {
  local relative deploy_asset_hash runtime_asset_hash
  git -C "$SOURCE_DIR" checkout --detach "$TARGET_SHA" >/dev/null || die target_checkout_failed
  SOURCE_SWITCHED=1
  [[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$TARGET_SHA" && -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]] || die target_checkout_invalid
  for relative in deploy/compose.release.yml deploy/caddy/Caddyfile deploy/caddy/caddy-entrypoint.sh; do
    [[ -f "$SOURCE_DIR/$relative" && ! -L "$SOURCE_DIR/$relative" ]] || die runtime_asset_missing
    [[ "$(sha256_file "$SOURCE_DIR/$relative")" == "$(sha256_file "$EURITH_DEPLOY_ASSET_ROOT/$relative")" ]] || die runtime_asset_bytes_mismatch
  done
  deploy_asset_hash="$(sha256sum "$EURITH_DEPLOY_ASSET_ROOT/deploy/compose.release.yml" "$EURITH_DEPLOY_ASSET_ROOT/deploy/caddy/Caddyfile" "$EURITH_DEPLOY_ASSET_ROOT/deploy/caddy/caddy-entrypoint.sh" | sha256sum | awk '{print $1}')"
  runtime_asset_hash="$(sha256sum "$SOURCE_DIR/deploy/compose.release.yml" "$SOURCE_DIR/deploy/caddy/Caddyfile" "$SOURCE_DIR/deploy/caddy/caddy-entrypoint.sh" | sha256sum | awk '{print $1}')"
  [[ "$runtime_asset_hash" == "$deploy_asset_hash" ]] || die runtime_asset_bytes_mismatch
  export EURITH_RUNTIME_SOURCE_ROOT="$SOURCE_DIR"
  export EURITH_RUNTIME_ASSET_ROOT="$SOURCE_DIR"
  "${compose[@]}" config >/dev/null || die runtime_compose_validation_failed
  evidence deploy_asset_sha256 "$deploy_asset_hash"
  evidence runtime_asset_sha256 "$runtime_asset_hash"
}

validate_caddy() {
  local compose_hash caddy_digest repo_digests
  compose_hash="$(sha256sum "$EURITH_BASE_COMPOSE" "$RELEASE_OVERLAY" "$CADDYFILE" | sha256sum | awk '{print $1}')"; evidence compose_sha256 "$compose_hash"
  "${compose[@]}" config >/dev/null || die compose_validation_failed
  docker pull "$CADDY_IMAGE_REF" >/dev/null || die caddy_pull_failed
  repo_digests="$(docker image inspect "$CADDY_IMAGE_REF" --format '{{join .RepoDigests " "}}')" || die caddy_digest_inspect_failed
  grep -Eq "(^|[[:space:]])[^[:space:]]+@${EURITH_APPROVED_CADDY_DIGEST}([[:space:]]|$)" <<<"$repo_digests" || die caddy_digest_mismatch
  caddy_digest="$EURITH_APPROVED_CADDY_DIGEST"
  evidence caddy_image "$CADDY_IMAGE"; evidence caddy_digest "$caddy_digest"
  docker run --rm -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE_REF" caddy fmt --diff /etc/caddy/Caddyfile >/dev/null || die caddy_format_invalid
  docker run --rm -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE_REF" caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile --validate >/dev/null || die caddy_adapt_invalid
  docker run --rm -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE_REF" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null || die caddy_validation_failed
}

run_caddy_integration() {
  [[ "${TEST_DATABASE_URL:-}" =~ ^postgresql\+asyncpg://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?/fitpilot_task_caddy_[a-z0-9_]+$ ]] || die guarded_caddy_database_required
  (cd "$EURITH_DEPLOY_ASSET_ROOT" && CADDY_INTEGRATION_REQUIRED=1 python3 -m pytest tests/deploy/test_caddy_integration.py -q -m caddy_integration) || die caddy_integration_failed
  evidence caddy_integration passed
}

gate_migrations() {
  local current heads migration_path migration_path_hash approval_output approval_hash approval_identity path_manifest
  current="$("${compose[@]}" exec -T api alembic current 2>/dev/null | sed -n 's/^\([0-9A-Za-z_]*\).*/\1/p' | tail -n1)" || die alembic_current_failed
  [[ "$current" =~ ^[0-9A-Za-z_]+$ ]] || die alembic_current_unknown
  path_manifest="$EVIDENCE_DIR/migration-path.manifest"
  python3 "$MIGRATION_MANIFEST_TOOL" "$EURITH_DEPLOY_ASSET_ROOT/migrations/versions" "$current" >"$path_manifest" || die alembic_graph_invalid
  chmod 0600 "$path_manifest" || die migration_manifest_permissions_failed
  heads="$(sed -n 's/^target_head=//p' "$path_manifest")"
  migration_path="$(sed -n 's/^migration_path=//p' "$path_manifest")"
  migration_path_hash="$(sed -n 's/^migration_path_sha256=//p' "$path_manifest")"
  [[ "$heads" =~ ^[0-9A-Za-z_]+$ && "$migration_path" == "$current->$heads" ]] || die alembic_path_unknown
  [[ "$migration_path_hash" =~ ^[0-9a-f]{64}$ ]] || die migration_path_hash_invalid
  approval_output="$(python3 "$MIGRATION_APPROVAL_TOOL" "$EURITH_MIGRATION_APPROVAL_FILE" "$OLD_COMMIT" "$TARGET_SHA" "$ROLLBACK_SHA" "$current" "$heads" "$migration_path_hash")" || die migration_approval_file_invalid
  approval_hash="$(sed -n 's/^approval_sha256=//p' <<<"$approval_output")"
  approval_identity="$(sed -n 's/^approval_identity=//p' <<<"$approval_output")"
  [[ "$approval_hash" =~ ^[0-9a-f]{64}$ && "$approval_identity" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]{2,127}$ ]] || die migration_approval_output_invalid
  EXPECTED_ALEMBIC_HEAD="$heads"
  evidence alembic_heads "$heads"; evidence alembic_path "$migration_path"; evidence migration_path_sha256 "$migration_path_hash"; evidence migration_policy_sha256 "$approval_hash"; evidence migration_approval_identity "$approval_identity"; evidence rehearsal_probe_sha256 "$(sha256_file "$REHEARSAL_PROBE")"
}

build_target() {
  local runtime_heads
  "$SCRIPT_DIR/provision-release-host.sh" --secret-source-dir "$EURITH_SECRET_SOURCE_DIR" --base-compose "$EURITH_BASE_COMPOSE" --release-overlay "$RELEASE_OVERLAY" --deploy-asset-root "$EURITH_DEPLOY_ASSET_ROOT" --target-sha "$TARGET_SHA" >/dev/null || die provisioning_verification_failed
  "${compose[@]}" build api || die target_build_failed
  runtime_heads="$("${compose[@]}" run --rm --no-deps api alembic heads 2>/dev/null | sed -n 's/^\([0-9A-Za-z_]*\).*/\1/p')" || die alembic_heads_failed
  [[ "$runtime_heads" == "$EXPECTED_ALEMBIC_HEAD" ]] || die built_image_alembic_head_mismatch
  evidence build_status passed
}

rehearse_migration_compatibility() {
  local target_current rollback_heads
  local -a target_rehearsal_compose rollback_candidate_compose
  target_rehearsal_compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$RELEASE_OVERLAY" -f "$REHEARSAL_OVERLAY")
  "${target_rehearsal_compose[@]}" config >/dev/null || die target_rehearsal_compose_invalid
  "${target_rehearsal_compose[@]}" run --rm --no-deps api alembic upgrade head || die target_migration_rehearsal_failed
  target_current="$("${target_rehearsal_compose[@]}" run --rm --no-deps api alembic current 2>/dev/null | sed -n 's/^\([0-9A-Za-z_]*\).*/\1/p' | tail -n1)" || die target_schema_rehearsal_failed
  [[ "$target_current" == "$EXPECTED_ALEMBIC_HEAD" ]] || die target_schema_rehearsal_failed
  "${target_rehearsal_compose[@]}" run --rm --no-deps --workdir /app -e PYTHONPATH=/app api python /tmp/eurith-rehearse-release-db.py "$EXPECTED_ALEMBIC_HEAD" || die target_schema_rehearsal_failed
  evidence target_migration_rehearsal passed
  git -C "$SOURCE_DIR" checkout --detach "$ROLLBACK_SHA" >/dev/null || die rollback_candidate_checkout_failed
  SOURCE_SWITCHED=1
  [[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$ROLLBACK_SHA" && -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]] || die rollback_candidate_checkout_invalid
  export EURITH_RUNTIME_SOURCE_ROOT="$SOURCE_DIR"
  export EURITH_RUNTIME_ASSET_ROOT="$EURITH_DEPLOY_ASSET_ROOT"
  rollback_candidate_compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$RELEASE_OVERLAY" -f "$REHEARSAL_OVERLAY")
  "${rollback_candidate_compose[@]}" config >/dev/null || die rollback_candidate_compatibility_rehearsal_failed
  "${rollback_candidate_compose[@]}" build api >/dev/null || die rollback_candidate_compatibility_rehearsal_failed
  [[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$ROLLBACK_SHA" && -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]] || die rollback_candidate_checkout_invalid
  rollback_heads="$("${rollback_candidate_compose[@]}" run --rm --no-deps api alembic heads 2>/dev/null | sed -n 's/^\([0-9A-Za-z_]*\).*/\1/p')" || die rollback_candidate_compatibility_rehearsal_failed
  [[ "$rollback_heads" == "$EXPECTED_ALEMBIC_HEAD" ]] || die rollback_candidate_alembic_head_mismatch
  "${rollback_candidate_compose[@]}" run --rm --no-deps --workdir /app -e PYTHONPATH=/app api python /tmp/eurith-rehearse-release-db.py "$EXPECTED_ALEMBIC_HEAD" || die rollback_candidate_compatibility_rehearsal_failed
  evidence rollback_candidate_compatibility_rehearsal passed
  export EURITH_RUNTIME_SOURCE_ROOT="$EURITH_DEPLOY_ASSET_ROOT"
  export EURITH_RUNTIME_ASSET_ROOT="$EURITH_DEPLOY_ASSET_ROOT"
  "${compose[@]}" build api >/dev/null || die target_rebuild_after_rehearsal_failed
  SCHEMA_ROLLBACK_COMPATIBLE=1
  evidence schema_rollback_compatible yes
}

apply_migration_once() {
  MIGRATION_ATTEMPTED=1; MIGRATION_STATE=unknown
  printf '%s\n' 'migration_state=unknown' >&2
  "${compose[@]}" run --rm --no-deps api alembic upgrade head || die migration_failed
  MIGRATION_STATE=applied; evidence migration_state applied; MIGRATION_EVIDENCE_WRITTEN=1
  "${compose[@]}" run --rm --no-deps api python scripts/localization_catalog_gate.py || die localization_gate_failed
  evidence migration_status passed
}

capture_prior_runtime() {
  local old_caddy_id
  old_caddy_id="$("${compose[@]}" ps -q caddy 2>/dev/null || true)"
  if [[ -n "$old_caddy_id" ]]; then
    PRIOR_CADDY_PRESENT=1
    OLD_CADDY_IMAGE_ID="$(docker inspect "$old_caddy_id" --format '{{.Image}}' 2>/dev/null || true)"
    [[ "$OLD_CADDY_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || die prior_caddy_image_unknown
    evidence prior_caddy_image_id "$OLD_CADDY_IMAGE_ID"
  fi
  evidence prior_caddy_present "$PRIOR_CADDY_PRESENT"
}

rollback_infrastructure() {
  local result=failed rollback_caddy_id rollback_caddy_image rollback_image_overlay=''
  local rollback_api_ref rollback_built_api_image rollback_api_id rollback_api_image rollback_api_status rollback_api_restarts
  local current_api_id attempt status rollback_api_image_verified=0 rollback_api_runtime_verified=0 rollback_api_readiness_verified=0 rollback_api_verified=0 rollback_caddy_verified=0
  local -a rollback_compose
  ROLLBACK_ATTEMPTED=1
  if [[ "$SWITCH_ATTEMPTED" == 1 || ( "$MIGRATION_ATTEMPTED" == 1 && "$MIGRATION_STATE" == applied ) ]]; then
    if [[ "$SWITCH_ATTEMPTED" == 1 ]]; then "${compose[@]}" stop caddy >/dev/null 2>&1 || true; fi
    if [[ "$PRIOR_CADDY_PRESENT" == 0 ]]; then
      if "${compose[@]}" rm -f caddy >/dev/null 2>&1; then evidence rollback_caddy_absent passed; else evidence rollback_caddy_absent failed; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; fi
    fi
    if ! git -C "$SOURCE_DIR" checkout --detach "$ROLLBACK_SHA" >/dev/null 2>&1; then evidence rollback_checkout failed; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; fi
    if [[ "$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null)" != "$ROLLBACK_SHA" ]]; then evidence rollback_checkout failed; evidence rollback_checkout_mismatch yes; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; fi
    if [[ -n "$(git -C "$SOURCE_DIR" status --porcelain 2>/dev/null)" ]]; then evidence rollback_checkout failed; evidence rollback_checkout_dirty yes; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; fi
    evidence rollback_checkout passed
    export EURITH_RUNTIME_SOURCE_ROOT="$SOURCE_DIR"
    export EURITH_RUNTIME_ASSET_ROOT="$EURITH_DEPLOY_ASSET_ROOT"
    if [[ "$MIGRATION_ATTEMPTED" == 0 || ( "$MIGRATION_STATE" == applied && "$SCHEMA_ROLLBACK_COMPATIBLE" == 1 ) ]]; then
      rollback_compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$RELEASE_OVERLAY")
      if [[ "$PRIOR_CADDY_PRESENT" == 1 ]]; then
        [[ "$OLD_CADDY_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || { evidence rollback_caddy_image failed; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; }
        rollback_image_overlay="$(mktemp)" || { evidence rollback_caddy_image failed; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; }
        chmod 0600 "$rollback_image_overlay" || { rm -f -- "$rollback_image_overlay"; evidence rollback_caddy_image failed; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; }
        printf 'services:\n  caddy:\n    image: "%s"\n' "$OLD_CADDY_IMAGE_ID" >"$rollback_image_overlay" || { rm -f -- "$rollback_image_overlay"; evidence rollback_caddy_image failed; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; }
        rollback_compose+=(-f "$rollback_image_overlay")
      else
        evidence rollback_caddy_image not_applicable
        rollback_caddy_verified=1
      fi
      if "${rollback_compose[@]}" build api >/dev/null 2>&1; then
        rollback_api_ref="$("${rollback_compose[@]}" config --images api 2>/dev/null || true)"
        if [[ "$rollback_api_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]]; then
          rollback_built_api_image="$(docker image inspect --format '{{.Id}}' "$rollback_api_ref" 2>/dev/null || true)"
          [[ "$rollback_built_api_image" == sha256:* ]] || rollback_built_api_image="sha256:${rollback_built_api_image}"
          if [[ "$rollback_built_api_image" =~ ^sha256:[0-9a-f]{64}$ ]]; then
            if [[ "$PRIOR_CADDY_PRESENT" == 1 ]]; then
              "${rollback_compose[@]}" up -d --no-deps --pull never api caddy >/dev/null 2>&1 || true
              rollback_caddy_id="$("${rollback_compose[@]}" ps -q caddy 2>/dev/null || true)"
              rollback_caddy_image="$(docker inspect "$rollback_caddy_id" --format '{{.Image}}' 2>/dev/null || true)"
              if [[ -n "$rollback_caddy_id" && "$rollback_caddy_image" == "$OLD_CADDY_IMAGE_ID" ]]; then rollback_caddy_verified=1; fi
            else
              "${rollback_compose[@]}" up -d --no-deps --pull never api >/dev/null 2>&1 || true
            fi
            rollback_api_id="$("${rollback_compose[@]}" ps -q api 2>/dev/null || true)"
            rollback_api_image="$(docker inspect "$rollback_api_id" --format '{{.Image}}' 2>/dev/null || true)"
            rollback_api_status="$(docker inspect "$rollback_api_id" --format '{{.State.Status}}' 2>/dev/null || true)"
            rollback_api_restarts="$(docker inspect "$rollback_api_id" --format '{{.RestartCount}}' 2>/dev/null || true)"
            if [[ -n "$rollback_api_id" && "$rollback_api_image" == "$rollback_built_api_image" ]]; then rollback_api_image_verified=1; fi
            if [[ "$rollback_api_image_verified" == 1 && "$rollback_api_status" == running && "$rollback_api_restarts" == 0 ]]; then
              rollback_api_runtime_verified=1
              for attempt in $(seq 1 30); do
                status="$(curl --silent --show-error --max-time 5 --max-filesize 65536 --output /dev/null --write-out '%{http_code}' "$PUBLIC_API_BASE/health" || true)"
                if [[ "$status" == 200 ]]; then
                  current_api_id="$("${rollback_compose[@]}" ps -q api 2>/dev/null || true)"
                  rollback_api_image="$(docker inspect "$current_api_id" --format '{{.Image}}' 2>/dev/null || true)"
                  rollback_api_status="$(docker inspect "$current_api_id" --format '{{.State.Status}}' 2>/dev/null || true)"
                  rollback_api_restarts="$(docker inspect "$current_api_id" --format '{{.RestartCount}}' 2>/dev/null || true)"
                  if [[ "$current_api_id" == "$rollback_api_id" && "$rollback_api_image" == "$rollback_built_api_image" && "$rollback_api_status" == running && "$rollback_api_restarts" == 0 ]]; then rollback_api_readiness_verified=1; else rollback_api_runtime_verified=0; fi
                  break
                fi
                sleep 2
              done
            fi
          fi
        fi
      fi
      [[ -z "$rollback_image_overlay" ]] || rm -f -- "$rollback_image_overlay"
      if [[ "$PRIOR_CADDY_PRESENT" == 1 ]]; then
        if [[ "$rollback_caddy_verified" == 1 ]]; then evidence rollback_caddy_image passed; else evidence rollback_caddy_image failed; fi
      fi
      if [[ "$rollback_api_image_verified" == 1 ]]; then evidence rollback_api_image passed; else evidence rollback_api_image failed; fi
      if [[ "$rollback_api_runtime_verified" == 1 ]]; then evidence rollback_api_runtime passed; else evidence rollback_api_runtime failed; fi
      if [[ "$rollback_api_readiness_verified" == 1 ]]; then evidence rollback_api_readiness passed; else evidence rollback_api_readiness failed; fi
      if [[ "$rollback_api_image_verified" == 1 && "$rollback_api_runtime_verified" == 1 && "$rollback_api_readiness_verified" == 1 ]]; then rollback_api_verified=1; fi
      [[ "$rollback_api_verified" == 1 && "$rollback_caddy_verified" == 1 ]] && result=passed
    fi
  fi
  evidence rollback_result "$result"; ROLLBACK_EVIDENCE_WRITTEN=1; printf '%s\n' 'database_restore=manual_only' >&2; return 1
}

switch_api_and_caddy() {
  local service container_id restarts
  SWITCH_ATTEMPTED=1
  "${compose[@]}" up -d --no-deps --pull never api caddy || rollback_infrastructure
  for service in api caddy; do
    container_id="$("${compose[@]}" ps -q "$service")"; [[ -n "$container_id" ]] || rollback_infrastructure
    restarts="$(docker inspect "$container_id" --format '{{.RestartCount}}' 2>/dev/null || printf invalid)"
    [[ "$restarts" == 0 ]] || { evidence unexpected_switched_container_restart yes; rollback_infrastructure; }
    SWITCH_CONTAINER_IDS["$service"]="$container_id"
  done
  evidence switch_status passed
}
verify_switched_container_stability() {
  local service container_id restarts
  for service in api caddy; do
    container_id="$("${compose[@]}" ps -q "$service")"
    [[ -n "$container_id" && "$container_id" == "${SWITCH_CONTAINER_IDS[$service]}" ]] || rollback_infrastructure
    restarts="$(docker inspect "$container_id" --format '{{.RestartCount}}' 2>/dev/null || printf invalid)"
    [[ "$restarts" == 0 ]] || { evidence unexpected_switched_container_restart yes; rollback_infrastructure; }
  done
  evidence switched_container_stability passed
}
wait_for_readiness() {
  local attempt status
  for attempt in $(seq 1 30); do
    status="$(curl --silent --show-error --max-time 5 --max-filesize 65536 --output /dev/null --write-out '%{http_code}' "$PUBLIC_API_BASE/health" || true)"
    if [[ "$status" == 200 ]]; then evidence readiness_attempts "$attempt"; return 0; fi
    sleep 2
  done
  rollback_infrastructure
}
run_public_canaries() {
  EURITH_PUBLIC_API_URL="$EURITH_PUBLIC_API_URL" EURITH_CANARY_IDS_FILE="$EURITH_CANARY_IDS_FILE" EURITH_BASE_COMPOSE="$EURITH_BASE_COMPOSE" RELEASE_OVERLAY="$RELEASE_OVERLAY" DEPLOY_ENV="$DEPLOY_ENV" "$SCRIPT_DIR/canary-release-delivery.sh" || rollback_infrastructure
  evidence canary_result passed
}
review_runtime_logs() {
  local logs; logs="$("${compose[@]}" logs --since 10m --tail 2000 --no-color api caddy 2>&1)" || rollback_infrastructure
  [[ "${#logs}" -le 1048576 ]] || rollback_infrastructure
  if grep -Eiq 'LocalProtocolError|Too little data for declared Content-Length|permission denied|release_view_gate=failed|[^0-9]5[0-9]{2}[^0-9]' <<<"$logs"; then rollback_infrastructure; fi
  evidence log_review passed
}
write_mobile_gate() {
  local completed_at
  completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; [[ "$completed_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || die deploy_timestamp_invalid
  evidence rollback_result not_required
  evidence backend_gate passed
  evidence deployment_result passed
  evidence deploy_completed_at "$completed_at"
  python3 "$MOBILE_GATE_PUBLISHER" "$EVIDENCE_DIR/deploy.env" "$MOBILE_GATE_FILE" "$TARGET_SHA" "$MOBILE_CANDIDATE_SHA" "$completed_at"
  printf 'backend_gate=passed\n'
}

on_exit() {
  local status=$?
  if [[ "$status" -ne 0 && -n "$EVIDENCE_DIR" ]]; then
    set +e
    if [[ "$MIGRATION_ATTEMPTED" == 1 && "$MIGRATION_EVIDENCE_WRITTEN" == 0 ]]; then evidence migration_state unknown; evidence manual_intervention_required yes; fi
    evidence "${CURRENT_STAGE}_exit" "$status"
    evidence deployment_result failed
    if [[ "$ROLLBACK_ATTEMPTED" == 0 && ( "$SWITCH_ATTEMPTED" == 1 || ( "$MIGRATION_ATTEMPTED" == 1 && "$MIGRATION_STATE" == applied ) ) ]]; then
      rollback_infrastructure
    elif [[ "$SWITCH_ATTEMPTED" == 0 ]]; then
      if [[ "$SOURCE_SWITCHED" == 1 ]]; then
        restore_sha="$OLD_COMMIT"; [[ "$MIGRATION_ATTEMPTED" == 1 ]] && restore_sha="$ROLLBACK_SHA"
        if git -C "$SOURCE_DIR" checkout --detach "$restore_sha" >/dev/null 2>&1 && [[ "$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null)" == "$restore_sha" ]] && [[ -z "$(git -C "$SOURCE_DIR" status --porcelain 2>/dev/null)" ]]; then
          evidence rollback_checkout passed
        else
          evidence rollback_checkout failed
        fi
      fi
      if [[ "$MIGRATION_ATTEMPTED" == 1 && "$MIGRATION_STATE" == unknown ]]; then evidence rollback_result manual_required; else evidence rollback_result not_required; fi
    fi
    if ! cleanup_rehearsal_inputs; then evidence rehearsal_input_cleanup failed; fi
    python3 - "$EVIDENCE_DIR/deploy.env" <<'PY'
import os, pathlib, sys
path = pathlib.Path(sys.argv[1])
with path.open("rb") as handle: os.fsync(handle.fileno())
for durable_directory in (path.parent, path.parent.parent):
    directory_fd = os.open(durable_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)
PY
  fi
  exit "$status"
}
trap on_exit EXIT
CURRENT_STAGE=gate_checkout; gate_checkout; evidence gate_checkout_exit 0
CURRENT_STAGE=gate_pause; gate_pause; evidence gate_pause_exit 0
CURRENT_STAGE=snapshot_rehearsal_inputs; snapshot_rehearsal_inputs; evidence snapshot_rehearsal_inputs_exit 0
CURRENT_STAGE=create_paired_backup; create_paired_backup; evidence create_paired_backup_exit 0
CURRENT_STAGE=verify_isolated_restore; verify_isolated_restore; evidence verify_isolated_restore_exit 0
CURRENT_STAGE=validate_caddy; validate_caddy; evidence validate_caddy_exit 0
CURRENT_STAGE=run_caddy_integration; run_caddy_integration; evidence run_caddy_integration_exit 0
CURRENT_STAGE=gate_migrations; gate_migrations; evidence gate_migrations_exit 0
CURRENT_STAGE=build_target; build_target; evidence build_target_exit 0
CURRENT_STAGE=rehearse_migration_compatibility; rehearse_migration_compatibility; evidence rehearse_migration_compatibility_exit 0
CURRENT_STAGE=checkout_target_source; checkout_target_source; evidence checkout_target_source_exit 0
CURRENT_STAGE=capture_prior_runtime; capture_prior_runtime; evidence capture_prior_runtime_exit 0
CURRENT_STAGE=apply_migration_once; apply_migration_once; evidence apply_migration_once_exit 0
CURRENT_STAGE=switch_api_and_caddy; switch_api_and_caddy; evidence switch_api_and_caddy_exit 0
CURRENT_STAGE=wait_for_readiness; wait_for_readiness; evidence wait_for_readiness_exit 0
CURRENT_STAGE=verify_switched_container_stability; verify_switched_container_stability; evidence verify_switched_container_stability_exit 0
CURRENT_STAGE=run_public_canaries; run_public_canaries; evidence run_public_canaries_exit 0
CURRENT_STAGE=review_runtime_logs; review_runtime_logs; evidence review_runtime_logs_exit 0
CURRENT_STAGE=verify_switched_container_stability_final; verify_switched_container_stability; evidence verify_switched_container_stability_final_exit 0
CURRENT_STAGE=cleanup_rehearsal_inputs; cleanup_rehearsal_inputs; evidence rehearsal_input_cleanup passed; evidence cleanup_rehearsal_inputs_exit 0
CURRENT_STAGE=write_mobile_gate; write_mobile_gate
trap - EXIT
