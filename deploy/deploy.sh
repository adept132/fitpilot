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
EURITH_MIGRATION_APPROVAL_FILE="${EURITH_MIGRATION_APPROVAL_FILE:-}"
EURITH_EVIDENCE_ROOT="${EURITH_EVIDENCE_ROOT:-$APP_DIR/deploy-evidence}"
EURITH_RELEASE_VOLUME_ROOT="${EURITH_RELEASE_VOLUME_ROOT:-$APP_DIR/releases}"
EURITH_SECRET_SOURCE_DIR="${EURITH_SECRET_SOURCE_DIR:-/etc/eurith/release-secret-source}"
APPROVED_REMOTE_REF="${APPROVED_REMOTE_REF:-origin/main}"
CADDY_IMAGE="caddy:2.11.4"
EURITH_APPROVED_CADDY_DIGEST="${EURITH_APPROVED_CADDY_DIGEST:-}"
CADDY_IMAGE_REF="$CADDY_IMAGE@$EURITH_APPROVED_CADDY_DIGEST"
export CADDY_IMAGE_REF
CADDYFILE="$RUNNER_ROOT/deploy/caddy/Caddyfile"
MOBILE_GATE_FILE="${MOBILE_GATE_FILE:-$EURITH_EVIDENCE_ROOT/backend-gate-${TARGET_SHA}.env}"

for command_name in git docker curl sha256sum awk sed grep stat findmnt head python3 install chmod date mktemp mv seq sleep; do require_command "$command_name"; done
for path in "$SOURCE_DIR" "$EURITH_BASE_COMPOSE" "$RELEASE_OVERLAY" "$DEPLOY_ENV" "$EURITH_BACKUP_ROOT" "$EURITH_RESTORE_DB_URL_FILE" "$EURITH_RESTORE_VOLUME_ROOT" "$EURITH_MIGRATION_APPROVAL_FILE" "$EURITH_EVIDENCE_ROOT" "$EURITH_CANARY_IDS_FILE" "$MOBILE_GATE_FILE"; do [[ -n "$path" ]] || die required_runtime_path_missing; done
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
[[ -f "$EURITH_CANARY_IDS_FILE" && ! -L "$EURITH_CANARY_IDS_FILE" ]] || die canary_ids_file_invalid
[[ -f "$EURITH_MIGRATION_APPROVAL_FILE" && ! -L "$EURITH_MIGRATION_APPROVAL_FILE" ]] || die migration_approval_file_invalid

require_safe_absolute_path "$EURITH_BACKUP_ROOT" backup "$SOURCE_DIR"
require_safe_absolute_path "$EURITH_EVIDENCE_ROOT" backup "$SOURCE_DIR"
require_safe_absolute_path "$EURITH_RESTORE_VOLUME_ROOT" backup "$SOURCE_DIR"
require_safe_absolute_path "$EURITH_MIGRATION_APPROVAL_FILE" secret "$SOURCE_DIR"
require_safe_absolute_path "$MOBILE_GATE_FILE" backup "$SOURCE_DIR"
case "$(_canonical_path "$EURITH_EVIDENCE_ROOT")/" in "$(_canonical_path "$EURITH_RELEASE_VOLUME_ROOT")"/*) die evidence_below_release_storage ;; esac
compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$RELEASE_OVERLAY")
OLD_COMMIT=''; EVIDENCE_DIR=''; BACKUP_GENERATION=''; BACKUP_MANIFEST_SHA256=''; EXPECTED_ALEMBIC_HEAD=''; MIGRATION_ATTEMPTED=0; MIGRATION_STATE=not_attempted; MIGRATION_EVIDENCE_WRITTEN=0; SWITCH_ATTEMPTED=0; SOURCE_SWITCHED=0; SCHEMA_ROLLBACK_COMPATIBLE=0; ROLLBACK_ATTEMPTED=0; ROLLBACK_EVIDENCE_WRITTEN=0; CURRENT_STAGE=preflight
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
  [[ -d "$RUNNER_ROOT/.git" || -f "$RUNNER_ROOT/.git" ]] || die deploy_runner_not_versioned
  [[ -z "$(git -C "$RUNNER_ROOT" status --porcelain)" ]] || die deploy_runner_dirty
  [[ "$(git -C "$RUNNER_ROOT" rev-parse HEAD)" == "$TARGET_SHA" ]] || die deploy_runner_sha_mismatch
  git fetch --prune origin >/dev/null || die fetch_failed
  [[ "$(git rev-parse --verify "${TARGET_SHA}^{commit}")" == "$TARGET_SHA" ]] || die target_sha_unresolved
  git rev-parse --verify "${APPROVED_REMOTE_REF}^{commit}" >/dev/null || die approved_remote_ref_missing
  git merge-base --is-ancestor "$TARGET_SHA" "$APPROVED_REMOTE_REF" || die target_not_in_approved_remote_ref
  if [[ ! -e "$EURITH_EVIDENCE_ROOT" ]]; then install -d -o 0 -g 0 -m 0700 -- "$EURITH_EVIDENCE_ROOT"; fi
  assert_mode_owner_group "$EURITH_EVIDENCE_ROOT" 700 0 0
  [[ "$(_canonical_path "$(dirname -- "$MOBILE_GATE_FILE")")" == "$(_canonical_path "$EURITH_EVIDENCE_ROOT")" ]] || die mobile_gate_parent_invalid
  [[ ! -e "$MOBILE_GATE_FILE" && ! -L "$MOBILE_GATE_FILE" ]] || die mobile_gate_already_exists
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
  BACKUP_MANIFEST_SHA256="$manifest_hash"; evidence backup_manifest_sha256 "$manifest_hash"
}

verify_isolated_restore() {
  local output restored_manifest
  output="$("$SCRIPT_DIR/verify-release-restore.sh" "$EURITH_BACKUP_ROOT/$BACKUP_GENERATION" "$EURITH_RESTORE_DB_URL_FILE" "$EURITH_RESTORE_VOLUME_ROOT")" || die isolated_restore_failed
  restored_manifest="$(sed -n 's/^manifest_sha256=//p' <<<"$output")"
  [[ "$restored_manifest" =~ ^[0-9a-f]{64}$ && "$restored_manifest" == "$BACKUP_MANIFEST_SHA256" ]] || die backup_restore_manifest_mismatch
  evidence restore_drill passed
}

checkout_target_source() {
  git -C "$SOURCE_DIR" checkout --detach "$TARGET_SHA" >/dev/null || die target_checkout_failed
  SOURCE_SWITCHED=1
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
  CADDY_INTEGRATION_REQUIRED=1 python3 -m pytest tests/deploy/test_caddy_integration.py -q -m caddy_integration || die caddy_integration_failed
  evidence caddy_integration passed
}

gate_migrations() {
  local current graph heads migration_path changed_migrations approval_hash
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
  [[ "$(stat -c '%a:%u:%g' "$EURITH_MIGRATION_APPROVAL_FILE")" == 400:0:0 ]] || die migration_approval_file_permissions_invalid
  mapfile -t approval_lines <"$EURITH_MIGRATION_APPROVAL_FILE"
  [[ "${#approval_lines[@]}" == 4 ]] || die migration_approval_file_invalid
  [[ "${approval_lines[0]}" == "old_backend_sha=$OLD_COMMIT" ]] || die migration_approval_file_old_sha_mismatch
  [[ "${approval_lines[1]}" == "new_backend_sha=$TARGET_SHA" ]] || die migration_approval_file_new_sha_mismatch
  [[ "${approval_lines[2]}" == "classification=additive" ]] || die migration_approval_file_policy_missing
  [[ "${approval_lines[3]}" == "rollback_rehearsal=passed" ]] || die migration_approval_file_rehearsal_missing
  approval_hash="$(sha256_file "$EURITH_MIGRATION_APPROVAL_FILE")"; [[ "$approval_hash" =~ ^[0-9a-f]{64}$ ]] || die migration_approval_file_hash_invalid
  EXPECTED_ALEMBIC_HEAD="$heads"
  SCHEMA_ROLLBACK_COMPATIBLE=1
  evidence alembic_heads "$heads"; evidence alembic_path "$migration_path"; evidence migration_policy_sha256 "$approval_hash"; evidence schema_rollback_compatible yes
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
  MIGRATION_ATTEMPTED=1; MIGRATION_STATE=unknown
  printf '%s\n' 'migration_state=unknown' >&2
  "${compose[@]}" run --rm --no-deps api alembic upgrade head || die migration_failed
  MIGRATION_STATE=applied; evidence migration_state applied; MIGRATION_EVIDENCE_WRITTEN=1
  "${compose[@]}" run --rm --no-deps api python scripts/localization_catalog_gate.py || die localization_gate_failed
  evidence migration_status passed
}

rollback_infrastructure() {
  local result=failed
  ROLLBACK_ATTEMPTED=1
  if [[ "$SWITCH_ATTEMPTED" == 1 ]]; then
    "${compose[@]}" stop caddy >/dev/null 2>&1 || true
    if ! git -C "$SOURCE_DIR" checkout --detach "$OLD_COMMIT" >/dev/null 2>&1; then evidence rollback_checkout failed; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; fi
    if [[ "$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null)" != "$OLD_COMMIT" ]]; then evidence rollback_checkout failed; evidence rollback_checkout_mismatch yes; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; fi
    if [[ -n "$(git -C "$SOURCE_DIR" status --porcelain 2>/dev/null)" ]]; then evidence rollback_checkout failed; evidence rollback_checkout_dirty yes; evidence rollback_result failed; ROLLBACK_EVIDENCE_WRITTEN=1; return 1; fi
    evidence rollback_checkout passed
    if [[ "$MIGRATION_ATTEMPTED" == 0 || ( "$MIGRATION_STATE" == applied && "$SCHEMA_ROLLBACK_COMPATIBLE" == 1 ) ]]; then
      if [[ -f "$OLD_RELEASE_OVERLAY" ]]; then
        rollback_compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$OLD_RELEASE_OVERLAY")
        "${rollback_compose[@]}" build api >/dev/null 2>&1 && "${rollback_compose[@]}" up -d --no-deps --pull never api caddy >/dev/null 2>&1 && result=passed
      else
        rollback_compose=(docker compose -f "$EURITH_BASE_COMPOSE")
        "${rollback_compose[@]}" build api >/dev/null 2>&1 && "${rollback_compose[@]}" up -d --no-deps api >/dev/null 2>&1 && result=passed
      fi
    fi
  fi
  evidence rollback_result "$result"; ROLLBACK_EVIDENCE_WRITTEN=1; printf '%s\n' 'database_restore=manual_only' >&2; return 1
}

switch_api_and_caddy() { SWITCH_ATTEMPTED=1; "${compose[@]}" up -d --no-deps --pull never api caddy || rollback_infrastructure; evidence switch_status passed; }
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
  python3 - "$EVIDENCE_DIR/deploy.env" "$MOBILE_GATE_FILE" "$TARGET_SHA" "$MOBILE_CANDIDATE_SHA" "$completed_at" <<'PY'
import hashlib, os, pathlib, sys, tempfile
evidence, gate = map(pathlib.Path, sys.argv[1:3])
backend, mobile, completed = sys.argv[3:6]
with evidence.open("rb") as handle:
    payload = handle.read(); os.fsync(handle.fileno())
evidence_sha256 = hashlib.sha256(payload).hexdigest()
directory = gate.parent
fd, temporary = tempfile.mkstemp(prefix=".backend-gate.", dir=directory)
try:
    os.fchmod(fd, 0o600)
    content = (
        f"backend_gate=passed\nbackend_sha={backend}\nmobile_candidate_sha={mobile}\n"
        f"deploy_completed_at={completed}\nevidence_sha256={evidence_sha256}\nwrite_mobile_gate_exit=0\n"
    ).encode("ascii")
    os.write(fd, content); os.fsync(fd); os.close(fd); fd = -1
    os.link(temporary, gate)
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)
finally:
    if fd >= 0: os.close(fd)
    try: os.unlink(temporary)
    except FileNotFoundError: pass
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)
PY
  printf 'backend_gate=passed\n'
}

on_exit() {
  local status=$?
  if [[ "$status" -ne 0 && -n "$EVIDENCE_DIR" ]]; then
    set +e
    if [[ "$MIGRATION_ATTEMPTED" == 1 && "$MIGRATION_EVIDENCE_WRITTEN" == 0 ]]; then evidence migration_state unknown; evidence manual_intervention_required yes; fi
    evidence "${CURRENT_STAGE}_exit" "$status"
    evidence deployment_result failed
    if [[ "$SWITCH_ATTEMPTED" == 1 && "$ROLLBACK_ATTEMPTED" == 0 ]]; then
      rollback_infrastructure
    elif [[ "$SWITCH_ATTEMPTED" == 0 ]]; then
      if [[ "$SOURCE_SWITCHED" == 1 ]]; then
        if git -C "$SOURCE_DIR" checkout --detach "$OLD_COMMIT" >/dev/null 2>&1 && [[ "$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null)" == "$OLD_COMMIT" ]] && [[ -z "$(git -C "$SOURCE_DIR" status --porcelain 2>/dev/null)" ]]; then
          evidence rollback_checkout passed
        else
          evidence rollback_checkout failed
        fi
      fi
      if [[ "$MIGRATION_ATTEMPTED" == 1 && "$MIGRATION_STATE" == unknown ]]; then evidence rollback_result manual_required; else evidence rollback_result not_required; fi
    fi
    python3 - "$EVIDENCE_DIR/deploy.env" <<'PY'
import os, pathlib, sys
path = pathlib.Path(sys.argv[1])
with path.open("rb") as handle: os.fsync(handle.fileno())
PY
  fi
  exit "$status"
}
trap on_exit EXIT
CURRENT_STAGE=gate_checkout; gate_checkout; evidence gate_checkout_exit 0
CURRENT_STAGE=gate_pause; gate_pause; evidence gate_pause_exit 0
CURRENT_STAGE=create_paired_backup; create_paired_backup; evidence create_paired_backup_exit 0
CURRENT_STAGE=verify_isolated_restore; verify_isolated_restore; evidence verify_isolated_restore_exit 0
CURRENT_STAGE=checkout_target_source; checkout_target_source; evidence checkout_target_source_exit 0
CURRENT_STAGE=validate_caddy; validate_caddy; evidence validate_caddy_exit 0
CURRENT_STAGE=run_caddy_integration; run_caddy_integration; evidence run_caddy_integration_exit 0
CURRENT_STAGE=gate_migrations; gate_migrations; evidence gate_migrations_exit 0
CURRENT_STAGE=build_target; build_target; evidence build_target_exit 0
CURRENT_STAGE=apply_migration_once; apply_migration_once; evidence apply_migration_once_exit 0
CURRENT_STAGE=switch_api_and_caddy; switch_api_and_caddy; evidence switch_api_and_caddy_exit 0
CURRENT_STAGE=wait_for_readiness; wait_for_readiness; evidence wait_for_readiness_exit 0
CURRENT_STAGE=run_public_canaries; run_public_canaries; evidence run_public_canaries_exit 0
CURRENT_STAGE=review_runtime_logs; review_runtime_logs; evidence review_runtime_logs_exit 0
CURRENT_STAGE=write_mobile_gate; write_mobile_gate
trap - EXIT
