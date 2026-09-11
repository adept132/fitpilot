#!/usr/bin/env bash
set -Eeuo pipefail
set +x

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RUNNER_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
source "$SCRIPT_DIR/lib/release_common.sh"

[[ $# == 2 ]] || die usage
TARGET_SHA="${1,,}"; MOBILE_CANDIDATE_SHA="${2,,}"
require_full_sha "$TARGET_SHA"
require_full_sha "$MOBILE_CANDIDATE_SHA"

APP_DIR="${APP_DIR:-/opt/eurith}"
SOURCE_DIR="${SOURCE_DIR:-$APP_DIR/backend}"
EURITH_BASE_COMPOSE="${EURITH_BASE_COMPOSE:-$APP_DIR/docker-compose.yml}"
RELEASE_OVERLAY="${RELEASE_OVERLAY:-$RUNNER_ROOT/deploy/compose.release.yml}"
OLD_RELEASE_OVERLAY="$SOURCE_DIR/deploy/compose.release.yml"
DEPLOY_ENV="${DEPLOY_ENV:-/etc/eurith/release-deploy.env}"
EURITH_PUBLIC_API_URL="${EURITH_PUBLIC_API_URL:-}"
EURITH_CANARY_IDS_FILE="${EURITH_CANARY_IDS_FILE:-/etc/eurith/release-canary-ids.env}"
EURITH_BACKUP_ROOT="${EURITH_BACKUP_ROOT:-$APP_DIR/backups/releases}"
EURITH_RESTORE_DB_URL_FILE="${EURITH_RESTORE_DB_URL_FILE:-}"
EURITH_RESTORE_VOLUME_ROOT="${EURITH_RESTORE_VOLUME_ROOT:-}"
EURITH_EVIDENCE_ROOT="${EURITH_EVIDENCE_ROOT:-$APP_DIR/deploy-evidence}"
EURITH_SECRET_SOURCE_DIR="${EURITH_SECRET_SOURCE_DIR:-/etc/eurith/release-secret-source}"
APPROVED_REMOTE_REF="${APPROVED_REMOTE_REF:-origin/main}"
CADDY_IMAGE="caddy:2.11.4"
EURITH_APPROVED_CADDY_DIGEST="${EURITH_APPROVED_CADDY_DIGEST:-}"
CADDYFILE="$RUNNER_ROOT/deploy/caddy/Caddyfile"
MOBILE_GATE_FILE="${MOBILE_GATE_FILE:-$EURITH_EVIDENCE_ROOT/backend-gate-${TARGET_SHA}.env}"

for command_name in git docker curl sha256sum awk sed grep stat findmnt head python3; do require_command "$command_name"; done
for path in "$SOURCE_DIR" "$EURITH_BASE_COMPOSE" "$RELEASE_OVERLAY" "$DEPLOY_ENV" "$EURITH_BACKUP_ROOT" "$EURITH_RESTORE_DB_URL_FILE" "$EURITH_RESTORE_VOLUME_ROOT" "$EURITH_EVIDENCE_ROOT" "$EURITH_CANARY_IDS_FILE" "$MOBILE_GATE_FILE"; do [[ -n "$path" ]] || die required_runtime_path_missing; done
[[ "$EURITH_PUBLIC_API_URL" =~ ^https://[^[:space:]/]+/?$ ]] || die invalid_public_api_url
[[ "$EURITH_APPROVED_CADDY_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die missing_caddy_digest
[[ -f "$DEPLOY_ENV" && ! -L "$DEPLOY_ENV" ]] || die protected_deploy_env_missing
[[ "$(stat -c '%a:%u' "$DEPLOY_ENV")" =~ ^(600|640):0$ ]] || die protected_deploy_env_permissions_invalid
[[ -f "$EURITH_BASE_COMPOSE" && ! -L "$EURITH_BASE_COMPOSE" ]] || die base_compose_invalid
[[ -f "$RELEASE_OVERLAY" && ! -L "$RELEASE_OVERLAY" ]] || die release_overlay_invalid
[[ -f "$EURITH_CANARY_IDS_FILE" && ! -L "$EURITH_CANARY_IDS_FILE" ]] || die canary_ids_file_invalid

require_safe_absolute_path "$EURITH_BACKUP_ROOT" backup "$SOURCE_DIR"
require_safe_absolute_path "$EURITH_EVIDENCE_ROOT" backup "$SOURCE_DIR"
require_safe_absolute_path "$EURITH_RESTORE_VOLUME_ROOT" backup "$SOURCE_DIR"
compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$RELEASE_OVERLAY")
OLD_COMMIT=''; EVIDENCE_DIR=''; BACKUP_GENERATION=''; EXPECTED_ALEMBIC_HEAD=''; MIGRATION_APPLIED=0; SWITCH_ATTEMPTED=0; SOURCE_SWITCHED=0; SCHEMA_ROLLBACK_COMPATIBLE=0
evidence() {
  local key="$1" value="$2"
  [[ "$key" =~ ^[a-z_]+$ && "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *'='* ]] || die invalid_evidence
  printf '%s=%s\n' "$key" "$value" >>"$EVIDENCE_DIR/deploy.env"
}

gate_checkout() {
  cd "$SOURCE_DIR"; [[ -d .git ]] || die checkout_missing
  [[ -z "$(git status --porcelain)" ]] || die checkout_dirty
  OLD_COMMIT="$(git rev-parse HEAD)"; require_full_sha "${OLD_COMMIT,,}"
  [[ -d "$RUNNER_ROOT/.git" || -f "$RUNNER_ROOT/.git" ]] || die deploy_runner_not_versioned
  [[ -z "$(git -C "$RUNNER_ROOT" status --porcelain)" ]] || die deploy_runner_dirty
  [[ "$(git -C "$RUNNER_ROOT" rev-parse HEAD)" == "$TARGET_SHA" ]] || die deploy_runner_sha_mismatch
  git fetch --prune origin >/dev/null || die fetch_failed
  [[ "$(git rev-parse --verify "${TARGET_SHA}^{commit}")" == "$TARGET_SHA" ]] || die target_sha_unresolved
  git rev-parse --verify "${APPROVED_REMOTE_REF}^{commit}" >/dev/null || die approved_remote_ref_missing
  git merge-base --is-ancestor "$TARGET_SHA" "$APPROVED_REMOTE_REF" || die target_not_in_approved_remote_ref
  mkdir -p -- "$EURITH_EVIDENCE_ROOT"
  EVIDENCE_DIR="$EURITH_EVIDENCE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$TARGET_SHA"
  [[ ! -e "$EVIDENCE_DIR" && ! -L "$EVIDENCE_DIR" ]] || die evidence_generation_exists
  install -d -m 0700 -- "$EVIDENCE_DIR"; : >"$EVIDENCE_DIR/deploy.env"; chmod 0600 "$EVIDENCE_DIR/deploy.env"
  evidence old_backend_sha "$OLD_COMMIT"; evidence new_backend_sha "$TARGET_SHA"; evidence mobile_candidate_sha "$MOBILE_CANDIDATE_SHA"
}

gate_pause() {
  [[ "${RELEASE_MUTATIONS_PAUSED:-}" == 1 ]] || die release_mutations_not_paused
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet eurith-release-cleanup.timer; then die cleanup_timer_still_active; fi
  evidence mutations_paused passed
}

create_paired_backup() {
  local output manifest_hash
  output="$(RELEASE_MUTATIONS_PAUSED=1 RELEASE_CHECKOUT_ROOT="$SOURCE_DIR" "$SCRIPT_DIR/backup-release-state.sh" "$OLD_COMMIT" "$EURITH_BACKUP_ROOT")" || die paired_backup_failed
  BACKUP_GENERATION="$(sed -n 's/^generation=//p' <<<"$output")"; manifest_hash="$(sed -n 's/^manifest_sha256=//p' <<<"$output")"
  [[ "$BACKUP_GENERATION" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{40}$ && "$manifest_hash" =~ ^[0-9a-f]{64}$ ]] || die backup_manifest_invalid
  evidence backup_manifest_sha256 "$manifest_hash"
}

verify_isolated_restore() {
  "$SCRIPT_DIR/verify-release-restore.sh" "$EURITH_BACKUP_ROOT/$BACKUP_GENERATION" "$EURITH_RESTORE_DB_URL_FILE" "$EURITH_RESTORE_VOLUME_ROOT" >/dev/null || die isolated_restore_failed
  evidence restore_drill passed
}

checkout_target_source() {
  git -C "$SOURCE_DIR" checkout --detach "$TARGET_SHA" >/dev/null || die target_checkout_failed
  SOURCE_SWITCHED=1
}

validate_caddy() {
  local compose_hash caddy_digest
  compose_hash="$(sha256sum "$EURITH_BASE_COMPOSE" "$RELEASE_OVERLAY" "$CADDYFILE" | sha256sum | awk '{print $1}')"; evidence compose_sha256 "$compose_hash"
  "${compose[@]}" config >/dev/null || die compose_validation_failed
  docker pull "$CADDY_IMAGE" >/dev/null || die caddy_pull_failed
  caddy_digest="$(docker image inspect "$CADDY_IMAGE" --format '{{join .RepoDigests " "}}' | grep -o 'sha256:[0-9a-f]\{64\}' | head -n1)"
  [[ "$caddy_digest" == "$EURITH_APPROVED_CADDY_DIGEST" ]] || die caddy_digest_mismatch
  evidence caddy_image "$CADDY_IMAGE"; evidence caddy_digest "$caddy_digest"
  docker run --rm -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" caddy fmt --diff /etc/caddy/Caddyfile >/dev/null || die caddy_format_invalid
  docker run --rm -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile --validate >/dev/null || die caddy_adapt_invalid
  docker run --rm -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null || die caddy_validation_failed
}

run_caddy_integration() {
  [[ "${TEST_DATABASE_URL:-}" =~ ^postgresql\+asyncpg://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?/fitpilot_task_caddy_[a-z0-9_]+$ ]] || die guarded_caddy_database_required
  CADDY_INTEGRATION_REQUIRED=1 python3 -m pytest tests/deploy/test_caddy_integration.py -q -m caddy_integration || die caddy_integration_failed
  evidence caddy_integration passed
}

gate_migrations() {
  local current graph heads migration_path changed_migrations
  changed_migrations="$(git diff --name-only "$OLD_COMMIT..$TARGET_SHA" -- migrations/versions)"
  while IFS= read -r migration_file; do
    [[ -n "$migration_file" ]] || continue
    python3 - "$SOURCE_DIR/$migration_file" <<'PY' || die destructive_migration_requires_expand_contract
import ast, pathlib, re, sys
tree = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"), filename=sys.argv[1])
upgrade = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"), None)
if upgrade is None: raise SystemExit(1)
for node in ast.walk(upgrade):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"drop_table", "drop_column", "drop_constraint"}:
        raise SystemExit(1)
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r"\b(DROP\s+(TABLE|COLUMN)|ALTER\s+[^;]*\sTYPE\b)", node.value, re.I):
        raise SystemExit(1)
PY
  done <<<"$changed_migrations"
  current="$("${compose[@]}" exec -T api alembic current 2>/dev/null | sed -n 's/^\([0-9A-Za-z_]*\).*/\1/p' | tail -n1)" || die alembic_current_failed
  [[ "$current" =~ ^[0-9A-Za-z_]+$ ]] || die alembic_current_unknown
  graph="$(python3 - "$SOURCE_DIR/migrations/versions" "$current" <<'PY'
import ast, pathlib, sys
root, current = pathlib.Path(sys.argv[1]), sys.argv[2]
revisions = {}
for path in root.glob("*.py"):
    values = {}
    for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    values[target.id] = ast.literal_eval(node.value)
    revision = values.get("revision")
    parent = values.get("down_revision")
    if not isinstance(revision, str) or revision in revisions: raise SystemExit(1)
    if parent is not None and not isinstance(parent, str): raise SystemExit(1)
    revisions[revision] = parent
parents = {parent for parent in revisions.values() if parent is not None}
heads = sorted(set(revisions) - parents)
if len(heads) != 1 or current not in revisions: raise SystemExit(1)
cursor, seen = heads[0], set()
while cursor != current:
    if cursor in seen or cursor not in revisions or revisions[cursor] is None: raise SystemExit(1)
    seen.add(cursor); cursor = revisions[cursor]
print(heads[0]); print(f"{current}->{heads[0]}")
PY
)" || die alembic_graph_invalid
  heads="$(sed -n '1p' <<<"$graph")"; migration_path="$(sed -n '2p' <<<"$graph")"
  [[ "$heads" =~ ^[0-9A-Za-z_]+$ && "$migration_path" == "$current->$heads" ]] || die alembic_path_unknown
  EXPECTED_ALEMBIC_HEAD="$heads"
  SCHEMA_ROLLBACK_COMPATIBLE=1
  evidence alembic_heads "$heads"; evidence alembic_path "$migration_path"; evidence schema_rollback_compatible yes
}

build_target() {
  local runtime_heads
  "$SCRIPT_DIR/provision-release-host.sh" --secret-source-dir "$EURITH_SECRET_SOURCE_DIR" --base-compose "$EURITH_BASE_COMPOSE" --release-overlay "$RELEASE_OVERLAY" >/dev/null || die provisioning_verification_failed
  "${compose[@]}" build api || die target_build_failed
  runtime_heads="$("${compose[@]}" run --rm --no-deps api alembic heads 2>/dev/null | sed -n 's/^\([0-9A-Za-z_]*\).*/\1/p')" || die alembic_heads_failed
  [[ "$runtime_heads" == "$EXPECTED_ALEMBIC_HEAD" ]] || die built_image_alembic_head_mismatch
  evidence build_status passed
}

apply_migration_once() {
  "${compose[@]}" run --rm --no-deps api alembic upgrade head || die migration_failed; MIGRATION_APPLIED=1
  "${compose[@]}" run --rm --no-deps api python scripts/localization_catalog_gate.py || die localization_gate_failed
  evidence migration_status passed
}

rollback_infrastructure() {
  local result=failed
  if [[ "$SWITCH_ATTEMPTED" == 1 ]]; then
    "${compose[@]}" stop caddy >/dev/null 2>&1 || true
    git -C "$SOURCE_DIR" checkout --detach "$OLD_COMMIT" >/dev/null 2>&1 || true
    if [[ "$MIGRATION_APPLIED" == 0 || "$SCHEMA_ROLLBACK_COMPATIBLE" == 1 ]]; then
      if [[ -f "$OLD_RELEASE_OVERLAY" ]]; then
        rollback_compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$OLD_RELEASE_OVERLAY")
        "${rollback_compose[@]}" build api >/dev/null 2>&1 && "${rollback_compose[@]}" up -d --no-deps api caddy >/dev/null 2>&1 && result=passed
      else
        rollback_compose=(docker compose -f "$EURITH_BASE_COMPOSE")
        "${rollback_compose[@]}" build api >/dev/null 2>&1 && "${rollback_compose[@]}" up -d --no-deps api >/dev/null 2>&1 && result=passed
      fi
    fi
  fi
  evidence rollback_result "$result"; printf '%s\n' 'database_restore=manual_only' >&2; return 1
}

switch_api_and_caddy() { SWITCH_ATTEMPTED=1; "${compose[@]}" up -d --no-deps api caddy || rollback_infrastructure; evidence switch_status passed; }
run_public_canaries() {
  EURITH_PUBLIC_API_URL="$EURITH_PUBLIC_API_URL" EURITH_CANARY_IDS_FILE="$EURITH_CANARY_IDS_FILE" EURITH_BASE_COMPOSE="$EURITH_BASE_COMPOSE" RELEASE_OVERLAY="$RELEASE_OVERLAY" DEPLOY_ENV="$DEPLOY_ENV" "$SCRIPT_DIR/canary-release-delivery.sh" || rollback_infrastructure
  evidence canary_result passed
}
review_runtime_logs() {
  local logs; logs="$("${compose[@]}" logs --since 10m --no-color api caddy 2>&1 | tail -n 2000 | tail -c 131072)" || rollback_infrastructure
  if grep -Eiq 'LocalProtocolError|Too little data for declared Content-Length|permission denied|release_view_gate=failed|[^0-9]5[0-9]{2}[^0-9]' <<<"$logs"; then rollback_infrastructure; fi
  evidence log_review passed
}
write_mobile_gate() {
  local temporary="$MOBILE_GATE_FILE.tmp.$$"
  (umask 077; printf 'backend_gate=passed\nbackend_sha=%s\nmobile_candidate_sha=%s\n' "$TARGET_SHA" "$MOBILE_CANDIDATE_SHA" >"$temporary")
  mv -T -- "$temporary" "$MOBILE_GATE_FILE"; evidence backend_gate passed; evidence rollback_result not_required; printf 'backend_gate=passed\n'
}

trap 'status=$?; if [[ $status -ne 0 && -n "$EVIDENCE_DIR" && "$SWITCH_ATTEMPTED" == 0 ]]; then if [[ "$SOURCE_SWITCHED" == 1 ]]; then git -C "$SOURCE_DIR" checkout --detach "$OLD_COMMIT" >/dev/null 2>&1 || true; fi; evidence rollback_result not_required; fi; exit $status' EXIT
gate_checkout
gate_pause
create_paired_backup
verify_isolated_restore
checkout_target_source
validate_caddy
run_caddy_integration
gate_migrations
build_target
apply_migration_once
switch_api_and_caddy
run_public_canaries
review_runtime_logs
write_mobile_gate
trap - EXIT
