#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CHECKOUT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
source "$SCRIPT_DIR/lib/release_common.sh"

ROOT_PREFIX=/
SECRET_SOURCE_DIR=/etc/eurith/release-secret-source
API_RELEASE_ENV=/etc/eurith/api-release.env
CLEANUP_ENV=/etc/eurith/release-cleanup.env
DEPLOY_ENV=/etc/eurith/release-deploy.env
BASE_COMPOSE="$CHECKOUT_ROOT/../docker-compose.yml"
RELEASE_OVERLAY="$CHECKOUT_ROOT/deploy/compose.release.yml"
SITE_ADDRESS=https://api.eurith.app

usage() { printf '%s\n' 'usage: provision-release-host.sh [protected path options]' >&2; exit 2; }
while [[ $# -gt 0 ]]; do
  [[ $# -ge 2 ]] || usage
  case "$1" in
    --root) ROOT_PREFIX="$2" ;;
    --secret-source-dir) SECRET_SOURCE_DIR="$2" ;;
    --api-release-env) API_RELEASE_ENV="$2" ;;
    --cleanup-env) CLEANUP_ENV="$2" ;;
    --deploy-env) DEPLOY_ENV="$2" ;;
    --base-compose) BASE_COMPOSE="$2" ;;
    --release-overlay) RELEASE_OVERLAY="$2" ;;
    --site-address) SITE_ADDRESS="$2" ;;
    *) usage ;;
  esac
  shift 2
done

for command_name in docker findmnt mount getent groupadd install chown stat ln readlink head awk sha256sum mv rm chmod dirname sed; do
  require_command "$command_name"
done
if [[ "$ROOT_PREFIX" != / ]]; then require_safe_absolute_path "$ROOT_PREFIX" mutable "$CHECKOUT_ROOT"; fi
for path in "$SECRET_SOURCE_DIR" "$API_RELEASE_ENV" "$CLEANUP_ENV" "$DEPLOY_ENV" "$BASE_COMPOSE" "$RELEASE_OVERLAY"; do
  purpose=mutable; [[ "$path" == "$SECRET_SOURCE_DIR" || "$path" == "$API_RELEASE_ENV" || "$path" == "$CLEANUP_ENV" || "$path" == "$DEPLOY_ENV" ]] && purpose=secret
  require_safe_absolute_path "$path" "$purpose" "$CHECKOUT_ROOT"
done
[[ -f "$BASE_COMPOSE" && ! -L "$BASE_COMPOSE" ]] || die base_compose_invalid
[[ -f "$RELEASE_OVERLAY" && ! -L "$RELEASE_OVERLAY" ]] || die release_overlay_invalid
_reject_newline "$SITE_ADDRESS"
[[ "$SITE_ADDRESS" =~ ^https://([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?)(:[0-9]{1,5})?$ ]] || die site_address_invalid
if [[ "$ROOT_PREFIX" == / ]]; then require_root; elif [[ "${EURITH_FAKE_SYMLINKS:-}" != 1 ]]; then die alternate_root_requires_test_harness; fi

rooted() { if [[ "$ROOT_PREFIX" == / ]]; then printf '%s\n' "$1"; else printf '%s%s\n' "${ROOT_PREFIX%/}" "$1"; fi; }
STORAGE_ROOT="$(rooted /opt/eurith/releases)"
STAGING_DIR="$STORAGE_ROOT/.staging"
FINAL_DIR="$STORAGE_ROOT/android/sha256"
VIEW_ROOT="$(rooted /opt/eurith/release-caddy-view)"
FINAL_VIEW="$VIEW_ROOT/android/sha256"
PROBE_VIEW="$VIEW_ROOT/.probe"
PROBE_REGULAR="$PROBE_VIEW/regular"
PROBE_EXTERNAL="$PROBE_VIEW/external"
PROBE_CONTENT=eurith-release-view-probe-v1
GROUP_NAME=eurith-releases
for path in "$STORAGE_ROOT" "$STAGING_DIR" "$FINAL_DIR" "$VIEW_ROOT" "$FINAL_VIEW" "$PROBE_VIEW"; do require_safe_absolute_path "$path" mutable "$CHECKOUT_ROOT"; done

group_state=existing
if ! group_record="$(getent group "$GROUP_NAME" 2>/dev/null)"; then
  groupadd --system "$GROUP_NAME" >/dev/null 2>&1 || die shared_group_create_failed
  group_state=created
  group_record="$(getent group "$GROUP_NAME" 2>/dev/null)" || die shared_group_resolution_failed
fi
[[ "$(awk -F: 'NF >= 1 {print $1}' <<<"$group_record")" == "$GROUP_NAME" ]] || die shared_group_name_mismatch
GROUP_GID="$(awk -F: 'NF >= 3 {print $3}' <<<"$group_record")"
[[ "$GROUP_GID" =~ ^[1-9][0-9]*$ ]] || die shared_group_gid_invalid

atomic_install_text() {
  local destination="$1" mode="$2" owner="$3" group="$4" content="$5" parent temporary source
  parent="$(dirname -- "$destination")"; [[ -d "$parent" && ! -L "$parent" ]] || die protected_parent_invalid
  temporary="${destination}.install.$$"; source="${destination}.source.$$"
  [[ ! -e "$temporary" && ! -e "$source" ]] || die atomic_install_collision
  umask 077
  trap 'rm -f -- "${source:-}" "${temporary:-}"' EXIT
  printf '%s' "$content" >"$source" || die atomic_source_write_failed
  if ! install -m "$mode" -o "$owner" -g "$group" -- "$source" "$temporary"; then rm -f -- "$source" "$temporary"; die atomic_install_failed; fi
  rm -f -- "$source"
  if ! mv -T -- "$temporary" "$destination"; then rm -f -- "$temporary"; die atomic_replace_failed; fi
  trap - EXIT
}

atomic_install_text "$DEPLOY_ENV" 0640 0 "$GROUP_GID" "RELEASE_SHARED_GID=${GROUP_GID}"$'\n'"EURITH_SITE_ADDRESS=${SITE_ADDRESS}"$'\n'
assert_mode_owner_group "$DEPLOY_ENV" 640 0 "$GROUP_GID"
compose=(docker compose --env-file "$DEPLOY_ENV" -f "$BASE_COMPOSE" -f "$RELEASE_OVERLAY")
API_UID="$("${compose[@]}" run --rm --no-deps --entrypoint id api -u 2>/dev/null)" || die api_uid_resolution_failed
[[ "$API_UID" =~ ^[1-9][0-9]*$ ]] || die api_uid_invalid

storage_state=verified
if [[ ! -e "$STORAGE_ROOT" ]]; then
  install -d -m 2770 -o "$API_UID" -g "$GROUP_GID" -- "$STORAGE_ROOT" "$STAGING_DIR" "$(dirname -- "$FINAL_DIR")" "$FINAL_DIR" || die storage_create_failed
  storage_state=created
fi

for directory in "$STORAGE_ROOT" "$STAGING_DIR" "$(dirname -- "$FINAL_DIR")" "$FINAL_DIR"; do
  [[ -d "$directory" && ! -L "$directory" ]] || die storage_tree_invalid
  assert_mode_owner_group "$directory" 2770 "$API_UID" "$GROUP_GID"
done

read_protected_line() {
  local file="$1" value
  [[ -f "$file" && ! -L "$file" ]] || die secret_source_invalid
  assert_mode_owner_group "$file" 400 0 0
  [[ "$(awk 'END {print NR}' "$file")" == 1 ]] || die secret_source_line_count_invalid
  IFS= read -r value <"$file" || [[ -n "$value" ]] || die secret_source_empty
  _reject_newline "$value"; [[ -n "$value" ]] || die secret_source_empty; printf '%s' "$value"
}
validate_token() { [[ "$1" =~ ^[0-9a-f]{64}$ ]] || die secret_token_invalid; }
validate_database_url() { [[ "$1" =~ ^postgresql(\+asyncpg)?://[^[:space:]]+$ ]] || die cleanup_database_url_invalid; }

validate_existing_api_env() {
  local file="$1" key publisher operator webhook storage extra
  assert_mode_owner_group "$file" 640 0 "$GROUP_GID"
  IFS='=' read -r key publisher < <(sed -n '1p' "$file"); [[ "$key" == RELEASE_PUBLISHER_TOKEN ]] || die api_env_keys_invalid
  IFS='=' read -r key operator < <(sed -n '2p' "$file"); [[ "$key" == RELEASE_OPERATOR_TOKEN ]] || die api_env_keys_invalid
  IFS='=' read -r key webhook < <(sed -n '3p' "$file"); [[ "$key" == GITHUB_WEBHOOK_SECRET ]] || die api_env_keys_invalid
  IFS='=' read -r key storage < <(sed -n '4p' "$file"); [[ "$key" == RELEASE_STORAGE_ROOT && "$storage" == /var/lib/eurith/releases ]] || die api_env_keys_invalid
  [[ "$(awk 'END {print NR}' "$file")" == 4 ]] || die api_env_keys_invalid
  validate_token "$publisher"; validate_token "$operator"; validate_token "$webhook"
  [[ "$publisher" != "$operator" && "$publisher" != "$webhook" && "$operator" != "$webhook" ]] || die api_env_tokens_not_distinct
}

validate_existing_cleanup_env() {
  local file="$1" key database storage extra
  assert_mode_owner_group "$file" 640 0 "$GROUP_GID"
  IFS='=' read -r key database < <(sed -n '1p' "$file"); [[ "$key" == DATABASE_URL && -n "$database" ]] || die cleanup_env_keys_invalid
  IFS='=' read -r key storage < <(sed -n '2p' "$file"); [[ "$key" == RELEASE_STORAGE_ROOT && "$storage" == /opt/eurith/releases ]] || die cleanup_env_keys_invalid
  [[ "$(awk 'END {print NR}' "$file")" == 2 ]] || die cleanup_env_keys_invalid
  validate_database_url "$database"
}

api_env_state=existing; cleanup_env_state=existing; api_exists=0; cleanup_exists=0
[[ -e "$API_RELEASE_ENV" || -L "$API_RELEASE_ENV" ]] && api_exists=1
[[ -e "$CLEANUP_ENV" || -L "$CLEANUP_ENV" ]] && cleanup_exists=1
if [[ "$api_exists" != "$cleanup_exists" ]]; then die protected_env_partial_installation
elif [[ "$api_exists" == 1 ]]; then
  if [[ "$ROOT_PREFIX" != / && "${EURITH_FAKE_DESTINATION_SYMLINK:-}" == "$API_RELEASE_ENV" ]]; then die protected_env_is_symlink; fi
  [[ ! -L "$API_RELEASE_ENV" && ! -L "$CLEANUP_ENV" ]] || die protected_env_is_symlink
  validate_existing_api_env "$API_RELEASE_ENV"; validate_existing_cleanup_env "$CLEANUP_ENV"
else
  publisher="$(read_protected_line "$SECRET_SOURCE_DIR/publisher-token")"
  operator="$(read_protected_line "$SECRET_SOURCE_DIR/operator-token")"
  webhook="$(read_protected_line "$SECRET_SOURCE_DIR/webhook-secret")"
  database_url="$(read_protected_line "$SECRET_SOURCE_DIR/cleanup-database-url")"
  validate_token "$publisher"; validate_token "$operator"; validate_token "$webhook"
  validate_database_url "$database_url"
  [[ "$publisher" != "$operator" && "$publisher" != "$webhook" && "$operator" != "$webhook" ]] || die source_tokens_not_distinct
  atomic_install_text "$API_RELEASE_ENV" 0640 0 "$GROUP_GID" "RELEASE_PUBLISHER_TOKEN=${publisher}"$'\n'"RELEASE_OPERATOR_TOKEN=${operator}"$'\n'"GITHUB_WEBHOOK_SECRET=${webhook}"$'\n'"RELEASE_STORAGE_ROOT=/var/lib/eurith/releases"$'\n'
  atomic_install_text "$CLEANUP_ENV" 0640 0 "$GROUP_GID" "DATABASE_URL=${database_url}"$'\n'"RELEASE_STORAGE_ROOT=/opt/eurith/releases"$'\n'
  unset publisher operator webhook database_url
  api_env_state=created; cleanup_env_state=created
  validate_existing_api_env "$API_RELEASE_ENV"; validate_existing_cleanup_env "$CLEANUP_ENV"
fi

view_state=verified
if [[ ! -e "$VIEW_ROOT" ]]; then
  install -d -m 2750 -o 0 -g "$GROUP_GID" -- "$VIEW_ROOT" "$(dirname -- "$FINAL_VIEW")" "$FINAL_VIEW" "$PROBE_VIEW" || die release_view_create_failed
  atomic_install_text "$PROBE_REGULAR" 0440 0 "$GROUP_GID" "$PROBE_CONTENT"$'\n'
  ln -s -- /etc/passwd "$PROBE_EXTERNAL" || die external_probe_create_failed
  chown -h "0:${GROUP_GID}" -- "$PROBE_EXTERNAL" || die external_probe_owner_failed
  mount --bind "$FINAL_DIR" "$FINAL_VIEW" || die final_view_bind_failed
  mount -o remount,bind,ro,nosymfollow "$FINAL_VIEW" || die final_view_protect_failed
  mount --bind "$PROBE_VIEW" "$PROBE_VIEW" || die probe_view_bind_failed
  mount -o remount,bind,ro,nosymfollow "$PROBE_VIEW" || die probe_view_protect_failed
  view_state=created
else
  for directory in "$VIEW_ROOT" "$(dirname -- "$FINAL_VIEW")" "$PROBE_VIEW"; do
    [[ -d "$directory" && ! -L "$directory" ]] || die release_view_tree_invalid
    assert_mode_owner_group "$directory" 2750 0 "$GROUP_GID"
  done
  [[ -d "$FINAL_VIEW" && ! -L "$FINAL_VIEW" ]] || die release_view_tree_invalid
  assert_mode_owner_group "$FINAL_VIEW" 2770 "$API_UID" "$GROUP_GID"
fi
assert_mode_owner_group "$PROBE_REGULAR" 440 0 "$GROUP_GID"
[[ "$(/usr/bin/head -n 1 "$PROBE_REGULAR" 2>/dev/null)" == "$PROBE_CONTENT" ]] || die regular_probe_content_invalid
[[ "$(readlink -- "$PROBE_EXTERNAL" 2>/dev/null)" == /etc/passwd ]] || die external_probe_target_invalid
if [[ "$ROOT_PREFIX" == / ]]; then [[ -L "$PROBE_EXTERNAL" ]] || die external_probe_type_invalid; fi
external_metadata="$(stat -c '%a:%u:%g' -- "$PROBE_EXTERNAL" 2>/dev/null)" || die external_probe_metadata_unreadable
[[ "$external_metadata" == "777:0:${GROUP_GID}" ]] || die external_probe_metadata_mismatch
assert_mount_vfs_options "$FINAL_VIEW"; assert_mount_vfs_options "$PROBE_VIEW"
final_source_identity="$(stat -c '%d:%i' -- "$FINAL_DIR" 2>/dev/null)" || die final_source_identity_missing
final_view_identity="$(stat -c '%d:%i' -- "$FINAL_VIEW" 2>/dev/null)" || die final_view_identity_missing
[[ "$final_source_identity" == "$final_view_identity" ]] || die final_view_source_mismatch
assert_regular_read_and_external_symlink_denied "$PROBE_REGULAR" "$PROBE_EXTERNAL"

compose_run() { "${compose[@]}" run --rm --no-deps "$@"; }
FIXTURE_NAME="$(printf 'f%.0s' {1..64}).apk"
api_probe='set -eu; umask 077; staging=/var/lib/eurith/releases/.staging/'"$FIXTURE_NAME"'.tmp; final=/var/lib/eurith/releases/android/sha256/'"$FIXTURE_NAME"'; printf probe >"$staging"; chmod 0600 "$staging"; mv -T "$staging" "$final"; chmod 0640 "$final"'
compose_run -e EURITH_PROBE_ACTION=api-stage --entrypoint /bin/sh api -c "$api_probe" >/dev/null 2>&1 || die api_staging_probe_failed
cleanup_fixture() { compose_run -e EURITH_PROBE_ACTION=api-remove --entrypoint /bin/sh api -c 'rm -f -- /var/lib/eurith/releases/android/sha256/'"$FIXTURE_NAME"' /var/lib/eurith/releases/android/sha256/.eurith-caddy-create-probe' >/dev/null 2>&1; }

container_gate='set -eu; for target in /srv/eurith/releases/android/sha256 /run/eurith-release-view-probe; do options=$(awk -v target="$target" '\''$5 == target {print $6; found=1; exit} END {if (!found) exit 1}'\'' /proc/self/mountinfo); case ",$options," in *,ro,*) ;; *) exit 1;; esac; case ",$options," in *,nosymfollow,*) ;; *) exit 1;; esac; done; head -c 1 /run/eurith-release-view-probe/regular >/dev/null; test "$(readlink /run/eurith-release-view-probe/external)" = /etc/passwd; ! head -c 1 /run/eurith-release-view-probe/external >/dev/null 2>&1'
if ! compose_run -e EURITH_PROBE_ACTION=container-gate --entrypoint /bin/sh caddy -c "$container_gate" >/dev/null 2>&1; then cleanup_fixture || true; die container_mount_probe_failed; fi
if ! compose_run -e EURITH_PROBE_ACTION=caddy-read --entrypoint /bin/sh caddy -c 'head -c 1 /srv/eurith/releases/android/sha256/'"$FIXTURE_NAME" >/dev/null 2>&1; then cleanup_fixture || true; die caddy_read_probe_failed; fi

mutation_failed=0
if compose_run -e EURITH_PROBE_ACTION=caddy-create --entrypoint /bin/sh caddy -c ': > /srv/eurith/releases/android/sha256/.eurith-caddy-create-probe' >/dev/null 2>&1; then mutation_failed=1; fi
if compose_run -e EURITH_PROBE_ACTION=caddy-replace --entrypoint /bin/sh caddy -c 'cp /etc/hosts /srv/eurith/releases/android/sha256/'"$FIXTURE_NAME" >/dev/null 2>&1; then mutation_failed=1; fi
if compose_run -e EURITH_PROBE_ACTION=caddy-chmod --entrypoint /bin/sh caddy -c 'chmod 0600 /srv/eurith/releases/android/sha256/'"$FIXTURE_NAME" >/dev/null 2>&1; then mutation_failed=1; fi
if compose_run -e EURITH_PROBE_ACTION=caddy-delete --entrypoint /bin/sh caddy -c 'rm -f /srv/eurith/releases/android/sha256/'"$FIXTURE_NAME" >/dev/null 2>&1; then mutation_failed=1; fi
cleanup_fixture || die api_cleanup_probe_failed
[[ "$mutation_failed" == 0 ]] || die caddy_mutation_probe_failed

redacted_status group "$group_state"
redacted_status storage "$storage_state"
redacted_status release_view "$view_state"
redacted_status api_env "$api_env_state"
redacted_status cleanup_env "$cleanup_env_state"
redacted_status deploy_env updated
redacted_status permissions verified
