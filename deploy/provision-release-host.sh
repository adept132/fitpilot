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
DEPLOY_ASSET_ROOT=
TARGET_SHA=

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
    --deploy-asset-root) DEPLOY_ASSET_ROOT="$2" ;;
    --target-sha) TARGET_SHA="${2,,}" ;;
    *) usage ;;
  esac
  shift 2
done

for command_name in docker findmnt mount umount getent groupadd install chown stat ln readlink head awk sha256sum mv rm rmdir chmod dirname sed find sort mktemp od tr cat cmp systemctl systemd-analyze; do
  require_command "$command_name"
done
if [[ "$ROOT_PREFIX" != / ]]; then require_safe_absolute_path "$ROOT_PREFIX" mutable "$CHECKOUT_ROOT"; fi
for path in "$SECRET_SOURCE_DIR" "$API_RELEASE_ENV" "$CLEANUP_ENV" "$DEPLOY_ENV" "$BASE_COMPOSE" "$RELEASE_OVERLAY" "$DEPLOY_ASSET_ROOT"; do
  purpose=mutable; [[ "$path" == "$SECRET_SOURCE_DIR" || "$path" == "$API_RELEASE_ENV" || "$path" == "$CLEANUP_ENV" || "$path" == "$DEPLOY_ENV" ]] && purpose=secret
  require_safe_absolute_path "$path" "$purpose" "$CHECKOUT_ROOT"
done
require_full_sha "$TARGET_SHA"
asset_resolved="$(_canonical_path "$DEPLOY_ASSET_ROOT")"
checkout_resolved="$(_canonical_path "$CHECKOUT_ROOT")"
[[ "$asset_resolved" == "$checkout_resolved" ]] || die deploy_asset_root_mismatch
[[ "$(_canonical_path "$RELEASE_OVERLAY")" == "$asset_resolved/deploy/compose.release.yml" ]] || die deploy_asset_overlay_mismatch
if [[ "$ROOT_PREFIX" == / ]]; then
  require_command git
  [[ -d "$DEPLOY_ASSET_ROOT/.git" || -f "$DEPLOY_ASSET_ROOT/.git" ]] || die deploy_asset_root_not_versioned
  [[ "$(git -C "$DEPLOY_ASSET_ROOT" rev-parse --show-toplevel 2>/dev/null)" == "$asset_resolved" ]] || die deploy_asset_root_toplevel_mismatch
  [[ "$(git -C "$DEPLOY_ASSET_ROOT" rev-parse HEAD 2>/dev/null)" == "$TARGET_SHA" ]] || die deploy_asset_root_sha_mismatch
  [[ -z "$(git -C "$DEPLOY_ASSET_ROOT" status --porcelain 2>/dev/null)" ]] || die deploy_asset_root_dirty
  if git -C "$DEPLOY_ASSET_ROOT" symbolic-ref -q HEAD >/dev/null 2>&1; then die deploy_asset_root_not_detached; fi
  [[ "${CADDY_IMAGE_REF:-}" =~ ^caddy:2\.11\.4@sha256:[0-9a-f]{64}$ ]] || die caddy_image_ref_not_pinned
fi
export EURITH_RUNTIME_SOURCE_ROOT="$DEPLOY_ASSET_ROOT"
export EURITH_RUNTIME_ASSET_ROOT="$DEPLOY_ASSET_ROOT"
[[ -f "$BASE_COMPOSE" && ! -L "$BASE_COMPOSE" ]] || die base_compose_invalid
[[ -f "$RELEASE_OVERLAY" && ! -L "$RELEASE_OVERLAY" ]] || die release_overlay_invalid
[[ -f "$DEPLOY_ASSET_ROOT/Dockerfile" && ! -L "$DEPLOY_ASSET_ROOT/Dockerfile" ]] || die target_dockerfile_invalid
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
PROBE_SOURCE="$(rooted /opt/eurith/release-caddy-probe-source)"
PROBE_REGULAR="$PROBE_VIEW/regular"
PROBE_EXTERNAL="$PROBE_VIEW/external"
SOURCE_REGULAR="$PROBE_SOURCE/regular"
SOURCE_EXTERNAL="$PROBE_SOURCE/external"
PROBE_CONTENT=eurith-release-view-probe-v1
GROUP_NAME=eurith-releases
BOOT_HELPER_SOURCE="$DEPLOY_ASSET_ROOT/deploy/systemd/eurith-release-views"
BOOT_UNIT_SOURCE="$DEPLOY_ASSET_ROOT/deploy/systemd/eurith-release-views.service"
DOCKER_DROP_IN_SOURCE="$DEPLOY_ASSET_ROOT/deploy/systemd/docker-eurith-release-views.conf"
BOOT_HELPER_DESTINATION="$(rooted /usr/local/libexec/eurith-release-views)"
BOOT_UNIT_DESTINATION="$(rooted /etc/systemd/system/eurith-release-views.service)"
DOCKER_DROP_IN_DESTINATION="$(rooted /etc/systemd/system/docker.service.d/eurith-release-views.conf)"
for path in "$STORAGE_ROOT" "$STAGING_DIR" "$FINAL_DIR" "$VIEW_ROOT" "$FINAL_VIEW" "$PROBE_VIEW" "$PROBE_SOURCE"; do require_safe_absolute_path "$path" mutable "$CHECKOUT_ROOT"; done
for path in "$BOOT_HELPER_DESTINATION" "$BOOT_UNIT_DESTINATION" "$DOCKER_DROP_IN_DESTINATION"; do require_safe_absolute_path "$path" mutable "$CHECKOUT_ROOT"; done

TEMP_FILES=()
CREATED_VIEW_FILES=()
CREATED_VIEW_DIRS=()
PROVISION_SUCCESS=0
BOOT_LAYOUT_MOUNTED=0
FIXTURE_MAY_EXIST=0
FIXTURE_OWNED=0
CADDY_CREATE_MAY_EXIST=0
PROBE_TOKEN=''
FIXTURE_NAME=''

cleanup_owned_fixture() { :; }
cleanup_on_exit() {
  local status=$? index
  set +e
  for ((index=${#TEMP_FILES[@]}-1; index>=0; index--)); do rm -f -- "${TEMP_FILES[index]}"; done
  if [[ "$FIXTURE_MAY_EXIST" == 1 && -n "$FIXTURE_NAME" ]] && declare -p compose >/dev/null 2>&1; then cleanup_owned_fixture >/dev/null 2>&1; fi
  if [[ "$PROVISION_SUCCESS" != 1 && "$BOOT_LAYOUT_MOUNTED" != 1 ]]; then
    for ((index=${#CREATED_VIEW_FILES[@]}-1; index>=0; index--)); do rm -f -- "${CREATED_VIEW_FILES[index]}"; done
    for ((index=${#CREATED_VIEW_DIRS[@]}-1; index>=0; index--)); do rmdir -- "${CREATED_VIEW_DIRS[index]}" >/dev/null 2>&1; done
  fi
  return "$status"
}
trap cleanup_on_exit EXIT

ensure_root_owned_directory() {
  local directory="$1"
  if [[ -e "$directory" || -L "$directory" ]]; then
    [[ -d "$directory" && ! -L "$directory" ]] || die boot_asset_parent_invalid
  else
    install -d -m 0755 -o 0 -g 0 -- "$directory" || die boot_asset_parent_create_failed
  fi
  assert_mode_owner_group "$directory" 755 0 0
}

install_boot_asset() {
  local source="$1" destination="$2" mode="$3" expected_name="$4" parent basename temporary
  [[ -f "$source" && ! -L "$source" ]] || die boot_asset_source_invalid
  [[ "$(_canonical_path "$source")" == "$asset_resolved/deploy/systemd/$expected_name" ]] || die boot_asset_source_path_mismatch
  parent="$(dirname -- "$destination")"
  ensure_root_owned_directory "$parent"
  if [[ "$ROOT_PREFIX" != / && "${EURITH_FAKE_BOOT_ASSET_SYMLINK:-}" == "$destination" ]]; then
    die boot_asset_invalid
  fi
  if [[ -e "$destination" || -L "$destination" ]]; then
    [[ -f "$destination" && ! -L "$destination" ]] || die boot_asset_invalid
    assert_mode_owner_group "$destination" "$mode" 0 0
    cmp -s -- "$source" "$destination" || die boot_asset_bytes_mismatch
    return
  fi
  basename="${destination##*/}"
  temporary="$(mktemp --tmpdir="$parent" ".${basename}.tmp.XXXXXXXXXX")" || die secure_temp_create_failed
  TEMP_FILES+=("$temporary")
  [[ -f "$temporary" && ! -L "$temporary" ]] || die secure_temp_invalid
  install -m "0$mode" -o 0 -g 0 -- "$source" "$temporary" || die boot_asset_install_failed
  assert_mode_owner_group "$temporary" "$mode" 0 0
  if ln -T -- "$temporary" "$destination" 2>/dev/null; then
    rm -f -- "$temporary" || die atomic_publish_cleanup_failed
    return
  fi
  [[ -f "$destination" && ! -L "$destination" ]] || die boot_asset_publish_failed
  assert_mode_owner_group "$destination" "$mode" 0 0
  cmp -s -- "$source" "$destination" || die boot_asset_bytes_mismatch
}

install_boot_asset "$BOOT_HELPER_SOURCE" "$BOOT_HELPER_DESTINATION" 755 eurith-release-views
install_boot_asset "$BOOT_UNIT_SOURCE" "$BOOT_UNIT_DESTINATION" 644 eurith-release-views.service
install_boot_asset "$DOCKER_DROP_IN_SOURCE" "$DOCKER_DROP_IN_DESTINATION" 644 docker-eurith-release-views.conf

group_state=existing
if ! group_record="$(getent group "$GROUP_NAME" 2>/dev/null)"; then
  groupadd --system "$GROUP_NAME" >/dev/null 2>&1 || die shared_group_create_failed
  group_state=created
  group_record="$(getent group "$GROUP_NAME" 2>/dev/null)" || die shared_group_resolution_failed
fi
[[ "$(awk -F: 'NF >= 1 {print $1}' <<<"$group_record")" == "$GROUP_NAME" ]] || die shared_group_name_mismatch
GROUP_GID="$(awk -F: 'NF >= 3 {print $3}' <<<"$group_record")"
[[ "$GROUP_GID" =~ ^[1-9][0-9]*$ ]] || die shared_group_gid_invalid

assert_mode_owner_group "$SECRET_SOURCE_DIR" 700 0 0
destination_parent="$(dirname -- "$API_RELEASE_ENV")"
[[ "$destination_parent" == "$(dirname -- "$CLEANUP_ENV")" && "$destination_parent" == "$(dirname -- "$DEPLOY_ENV")" ]] || die protected_env_parents_differ
assert_mode_owner_group "$destination_parent" 750 0 "$GROUP_GID"
secret_inventory="$(find "$SECRET_SOURCE_DIR" -mindepth 1 -maxdepth 1 -printf '%f|%y\n' | sort)" || die secret_source_inventory_failed
expected_secret_inventory=$'cleanup-database-url|f\noperator-token|f\npublisher-token|f\nwebhook-secret|f'
[[ "$secret_inventory" == "$expected_secret_inventory" ]] || die secret_source_inventory_invalid

atomic_install_text() {
  local destination="$1" mode="$2" owner="$3" group="$4" content="$5" allow_replace="${6:-0}" parent_mode="${7:-750}" parent temporary basename normalized_mode
  normalized_mode="${mode#0}"
  parent="$(dirname -- "$destination")"; assert_mode_owner_group "$parent" "$parent_mode" 0 "$GROUP_GID"
  if [[ -e "$destination" || -L "$destination" ]]; then
    [[ "$allow_replace" == 1 && -f "$destination" && ! -L "$destination" ]] || die protected_destination_exists
    assert_mode_owner_group "$destination" "$normalized_mode" "$owner" "$group"
  fi
  basename="${destination##*/}"
  temporary="$(mktemp --tmpdir="$parent" ".${basename}.tmp.XXXXXXXXXX")" || die secure_temp_create_failed
  TEMP_FILES+=("$temporary")
  [[ -f "$temporary" && ! -L "$temporary" ]] || die secure_temp_invalid
  umask 077; printf '%s' "$content" >"$temporary" || die atomic_source_write_failed
  [[ -f "$temporary" && ! -L "$temporary" ]] || die secure_temp_replaced
  chmod "$mode" -- "$temporary" || die atomic_mode_failed
  chown "${owner}:${group}" -- "$temporary" || die atomic_owner_failed
  assert_mode_owner_group "$temporary" "$normalized_mode" "$owner" "$group"
  mv -T -- "$temporary" "$destination" || die atomic_replace_failed
}

atomic_install_text "$DEPLOY_ENV" 0640 0 "$GROUP_GID" "RELEASE_SHARED_GID=${GROUP_GID}"$'\n'"EURITH_SITE_ADDRESS=${SITE_ADDRESS}"$'\n' 1
assert_mode_owner_group "$DEPLOY_ENV" 640 0 "$GROUP_GID"

TARGET_API_IMAGE="$(docker build --quiet --file "$DEPLOY_ASSET_ROOT/Dockerfile" "$DEPLOY_ASSET_ROOT" 2>/dev/null)" || die target_api_image_build_failed
[[ "$TARGET_API_IMAGE" =~ ^(sha256:)?[0-9a-f]{64}$ ]] || die target_api_image_id_invalid
[[ "$TARGET_API_IMAGE" == sha256:* ]] || TARGET_API_IMAGE="sha256:${TARGET_API_IMAGE}"
API_UID="$(docker run --rm --entrypoint id "$TARGET_API_IMAGE" -u 2>/dev/null)" || die api_uid_resolution_failed
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

compose=(docker compose --env-file "$DEPLOY_ENV" -f "$BASE_COMPOSE" -f "$RELEASE_OVERLAY")

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
create_view_dir() {
  local directory="$1"
  [[ ! -e "$directory" && ! -L "$directory" ]] || die release_view_partial_state
  install -d -m 2750 -o 0 -g "$GROUP_GID" -- "$directory" || die release_view_create_failed
  CREATED_VIEW_DIRS+=("$directory")
}
view_exists=0; probe_source_exists=0
[[ -e "$VIEW_ROOT" || -L "$VIEW_ROOT" ]] && view_exists=1
[[ -e "$PROBE_SOURCE" || -L "$PROBE_SOURCE" ]] && probe_source_exists=1
[[ "$view_exists" == "$probe_source_exists" ]] || die release_view_partial_state
if [[ "$view_exists" == 0 ]]; then
  create_view_dir "$VIEW_ROOT"
  create_view_dir "$(dirname -- "$FINAL_VIEW")"
  create_view_dir "$FINAL_VIEW"
  create_view_dir "$PROBE_VIEW"
  create_view_dir "$PROBE_SOURCE"
  atomic_install_text "$SOURCE_REGULAR" 0440 0 "$GROUP_GID" "$PROBE_CONTENT"$'\n' 0 2750
  CREATED_VIEW_FILES+=("$SOURCE_REGULAR")
  ln -s -- /etc/passwd "$SOURCE_EXTERNAL" || die external_probe_create_failed
  CREATED_VIEW_FILES+=("$SOURCE_EXTERNAL")
  chown -h "0:${GROUP_GID}" -- "$SOURCE_EXTERNAL" || die external_probe_owner_failed
  view_state=created
else
  for directory in "$VIEW_ROOT" "$(dirname -- "$FINAL_VIEW")" "$PROBE_SOURCE"; do
    [[ -d "$directory" && ! -L "$directory" ]] || die release_view_tree_invalid
    assert_mode_owner_group "$directory" 2750 0 "$GROUP_GID"
  done
fi
probe_inventory="$(find "$PROBE_SOURCE" -mindepth 1 -maxdepth 1 -printf '%f|%y\n' | sort)" || die probe_inventory_failed
[[ "$probe_inventory" == $'external|l\nregular|f' ]] || die probe_inventory_invalid
assert_mode_owner_group "$SOURCE_REGULAR" 440 0 "$GROUP_GID"
[[ "$(/usr/bin/head -n 1 "$SOURCE_REGULAR" 2>/dev/null)" == "$PROBE_CONTENT" ]] || die regular_probe_content_invalid
[[ "$(readlink -- "$SOURCE_EXTERNAL" 2>/dev/null)" == /etc/passwd ]] || die external_probe_target_invalid
if [[ "$ROOT_PREFIX" == / ]]; then [[ -L "$SOURCE_EXTERNAL" ]] || die external_probe_type_invalid; fi
external_metadata="$(stat -c '%a:%u:%g' -- "$SOURCE_EXTERNAL" 2>/dev/null)" || die external_probe_metadata_unreadable
[[ "$external_metadata" == "777:0:${GROUP_GID}" ]] || die external_probe_metadata_mismatch

BOOT_HELPER_RUNNER="$BOOT_HELPER_DESTINATION"
if [[ "$ROOT_PREFIX" != / ]]; then
  BOOT_HELPER_RUNNER="${EURITH_TEST_BOOT_HELPER:-}"
  [[ -n "$BOOT_HELPER_RUNNER" ]] || die alternate_root_boot_helper_missing
  require_safe_absolute_path "$BOOT_HELPER_RUNNER" mutable "$CHECKOUT_ROOT"
  [[ -f "$BOOT_HELPER_RUNNER" && ! -L "$BOOT_HELPER_RUNNER" ]] || die alternate_root_boot_helper_invalid
fi
systemctl daemon-reload || die boot_systemd_reload_failed
systemd-analyze verify "$BOOT_UNIT_DESTINATION" docker.service >/dev/null 2>&1 || die boot_systemd_verify_failed
systemctl enable eurith-release-views.service >/dev/null 2>&1 || die boot_unit_enable_failed
"$BOOT_HELPER_RUNNER"
BOOT_LAYOUT_MOUNTED=1
systemctl is-enabled --quiet eurith-release-views.service || die boot_unit_not_enabled

assert_exact_bind_mount() {
  local source="$1" target="$2" reported_target source_identity target_identity
  reported_target="$(findmnt -n -o TARGET --mountpoint "$target" 2>/dev/null)" || die protected_mountpoint_missing
  [[ "$(_canonical_path "$reported_target")" == "$(_canonical_path "$target")" ]] || die protected_mountpoint_mismatch
  assert_mount_vfs_options "$target"
  source_identity="$(stat -c '%d:%i' -- "$source" 2>/dev/null)" || die bind_source_identity_missing
  target_identity="$(stat -c '%d:%i' -- "$target" 2>/dev/null)" || die bind_target_identity_missing
  [[ "$source_identity" == "$target_identity" ]] || die protected_bind_source_mismatch
}
assert_exact_bind_mount "$FINAL_DIR" "$FINAL_VIEW"
assert_exact_bind_mount "$PROBE_SOURCE" "$PROBE_VIEW"
assert_mode_owner_group "$FINAL_VIEW" 2770 "$API_UID" "$GROUP_GID"
assert_mode_owner_group "$PROBE_VIEW" 2750 0 "$GROUP_GID"
assert_mode_owner_group "$PROBE_REGULAR" 440 0 "$GROUP_GID"
[[ "$(/usr/bin/head -n 1 "$PROBE_REGULAR" 2>/dev/null)" == "$PROBE_CONTENT" ]] || die regular_probe_content_invalid
[[ "$(readlink -- "$PROBE_EXTERNAL" 2>/dev/null)" == /etc/passwd ]] || die external_probe_target_invalid
if [[ "$ROOT_PREFIX" == / ]]; then [[ -L "$PROBE_EXTERNAL" ]] || die external_probe_type_invalid; fi
external_metadata="$(stat -c '%a:%u:%g' -- "$PROBE_EXTERNAL" 2>/dev/null)" || die external_probe_metadata_unreadable
[[ "$external_metadata" == "777:0:${GROUP_GID}" ]] || die external_probe_metadata_mismatch
assert_regular_read_and_external_symlink_denied "$PROBE_REGULAR" "$PROBE_EXTERNAL"

"${compose[@]}" build api >/dev/null 2>&1 || die target_api_compose_build_failed
COMPOSE_API_REF="$("${compose[@]}" config --images api 2>/dev/null)" || die compose_api_image_ref_resolution_failed
[[ "$COMPOSE_API_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]] || die compose_api_image_ref_invalid
COMPOSE_API_IMAGE="$(docker image inspect --format '{{.Id}}' "$COMPOSE_API_REF" 2>/dev/null)" || die compose_api_image_resolution_failed
[[ "$COMPOSE_API_IMAGE" =~ ^(sha256:)?[0-9a-f]{64}$ ]] || die compose_api_image_id_invalid
[[ "$COMPOSE_API_IMAGE" == sha256:* ]] || COMPOSE_API_IMAGE="sha256:${COMPOSE_API_IMAGE}"
[[ "$COMPOSE_API_IMAGE" == "$TARGET_API_IMAGE" ]] || die compose_api_image_mismatch
COMPOSE_API_UID="$(docker run --rm --entrypoint id "$COMPOSE_API_IMAGE" -u 2>/dev/null)" || die compose_api_uid_resolution_failed
[[ "$COMPOSE_API_UID" =~ ^[1-9][0-9]*$ ]] || die compose_api_uid_invalid
[[ "$COMPOSE_API_UID" == "$API_UID" ]] || die compose_api_uid_mismatch
compose_run() { "${compose[@]}" run --rm --no-deps "$@"; }
if [[ "$ROOT_PREFIX" != / && -n "${EURITH_TEST_PROBE_TOKEN:-}" ]]; then
  PROBE_TOKEN="$EURITH_TEST_PROBE_TOKEN"
else
  PROBE_TOKEN="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" || die probe_token_generation_failed
fi
[[ "$PROBE_TOKEN" =~ ^[0-9a-f]{32}$ ]] || die probe_token_invalid
FIXTURE_NAME=".eurith-permission-probe-${PROBE_TOKEN}"
CADDY_CREATE_NAME=".eurith-caddy-create-${PROBE_TOKEN}"

cleanup_owned_fixture() {
  local cleanup_probe
  cleanup_probe='set -eu
final=/var/lib/eurith/releases/android/sha256/$EURITH_PROBE_NAME
staging=/var/lib/eurith/releases/.staging/$EURITH_PROBE_NAME.tmp
caddy_created=/var/lib/eurith/releases/android/sha256/$EURITH_CADDY_CREATE_NAME
expected=eurith-provision:$EURITH_PROBE_TOKEN
caddy_expected=caddy:$EURITH_PROBE_TOKEN
if [ -e "$staging" ] || [ -L "$staging" ]; then [ -f "$staging" ] && [ ! -L "$staging" ] && [ "$(cat "$staging")" = "$expected" ] || exit 1; rm -f -- "$staging"; fi
if [ -e "$final" ] || [ -L "$final" ]; then [ -f "$final" ] && [ ! -L "$final" ] || exit 1; if [ "$EURITH_FIXTURE_OWNED" = 1 ] || [ "$(cat "$final")" = "$expected" ]; then rm -f -- "$final"; else exit 1; fi; fi
if [ -e "$caddy_created" ] || [ -L "$caddy_created" ]; then [ -f "$caddy_created" ] && [ ! -L "$caddy_created" ] && [ "$(cat "$caddy_created")" = "$caddy_expected" ] || exit 1; rm -f -- "$caddy_created"; fi'
  compose_run -e EURITH_PROBE_ACTION=api-remove -e "EURITH_PROBE_NAME=$FIXTURE_NAME" -e "EURITH_PROBE_TOKEN=$PROBE_TOKEN" -e "EURITH_CADDY_CREATE_NAME=$CADDY_CREATE_NAME" -e "EURITH_FIXTURE_OWNED=$FIXTURE_OWNED" --entrypoint /bin/sh api -c "$cleanup_probe"
}

api_probe='set -eu
[ "$EURITH_PROBE_NAME" = ".eurith-permission-probe-$EURITH_PROBE_TOKEN" ]
staging=/var/lib/eurith/releases/.staging/$EURITH_PROBE_NAME.tmp
final=/var/lib/eurith/releases/android/sha256/$EURITH_PROBE_NAME
[ ! -e "$staging" ] && [ ! -L "$staging" ] && [ ! -e "$final" ] && [ ! -L "$final" ]
umask 077; set -C; printf "eurith-provision:%s" "$EURITH_PROBE_TOKEN" >"$staging"; set +C
chmod 0600 "$staging"
mv -T -n "$staging" "$final"
[ ! -e "$staging" ] && [ ! -L "$staging" ] && [ -f "$final" ] && [ ! -L "$final" ]
[ "$(cat "$final")" = "eurith-provision:$EURITH_PROBE_TOKEN" ]
chmod 0640 "$final"'
FIXTURE_MAY_EXIST=1
compose_run -e EURITH_PROBE_ACTION=api-stage -e "EURITH_PROBE_NAME=$FIXTURE_NAME" -e "EURITH_PROBE_TOKEN=$PROBE_TOKEN" --entrypoint /bin/sh api -c "$api_probe" >/dev/null 2>&1 || die api_staging_probe_failed
FIXTURE_OWNED=1

container_gate='set -eu; for target in /srv/eurith/releases/android/sha256 /run/eurith-release-view-probe; do options=$(awk -v target="$target" '\''$5 == target {print $6; found=1; exit} END {if (!found) exit 1}'\'' /proc/self/mountinfo); case ",$options," in *,ro,*) ;; *) exit 1;; esac; case ",$options," in *,nosymfollow,*) ;; *) exit 1;; esac; done; head -c 1 /run/eurith-release-view-probe/regular >/dev/null; test "$(readlink /run/eurith-release-view-probe/external)" = /etc/passwd; ! head -c 1 /run/eurith-release-view-probe/external >/dev/null 2>&1'
if ! compose_run -e EURITH_PROBE_ACTION=container-gate --entrypoint /bin/sh caddy -c "$container_gate" >/dev/null 2>&1; then die container_mount_probe_failed; fi
if ! compose_run -e EURITH_PROBE_ACTION=caddy-read -e "EURITH_PROBE_NAME=$FIXTURE_NAME" -e "EURITH_PROBE_TOKEN=$PROBE_TOKEN" --entrypoint /bin/sh caddy -c '[ "$(cat /srv/eurith/releases/android/sha256/$EURITH_PROBE_NAME)" = "eurith-provision:$EURITH_PROBE_TOKEN" ]' >/dev/null 2>&1; then die caddy_read_probe_failed; fi

mutation_failed=0
if compose_run -e EURITH_PROBE_ACTION=caddy-create -e "EURITH_CADDY_CREATE_NAME=$CADDY_CREATE_NAME" -e "EURITH_PROBE_TOKEN=$PROBE_TOKEN" --entrypoint /bin/sh caddy -c 'set -C; printf "caddy:%s" "$EURITH_PROBE_TOKEN" >"/srv/eurith/releases/android/sha256/$EURITH_CADDY_CREATE_NAME"' >/dev/null 2>&1; then mutation_failed=1; fi
if compose_run -e EURITH_PROBE_ACTION=caddy-replace -e "EURITH_PROBE_NAME=$FIXTURE_NAME" --entrypoint /bin/sh caddy -c 'cp /etc/hosts "/srv/eurith/releases/android/sha256/$EURITH_PROBE_NAME"' >/dev/null 2>&1; then mutation_failed=1; fi
if compose_run -e EURITH_PROBE_ACTION=caddy-chmod -e "EURITH_PROBE_NAME=$FIXTURE_NAME" --entrypoint /bin/sh caddy -c 'chmod 0600 "/srv/eurith/releases/android/sha256/$EURITH_PROBE_NAME"' >/dev/null 2>&1; then mutation_failed=1; fi
if compose_run -e EURITH_PROBE_ACTION=caddy-delete -e "EURITH_PROBE_NAME=$FIXTURE_NAME" --entrypoint /bin/sh caddy -c 'rm -f -- "/srv/eurith/releases/android/sha256/$EURITH_PROBE_NAME"' >/dev/null 2>&1; then mutation_failed=1; fi
cleanup_owned_fixture >/dev/null 2>&1 || die api_cleanup_probe_failed
FIXTURE_MAY_EXIST=0; FIXTURE_OWNED=0
[[ "$mutation_failed" == 0 ]] || die caddy_mutation_probe_failed

redacted_status group "$group_state"
redacted_status storage "$storage_state"
redacted_status release_view "$view_state"
redacted_status api_env "$api_env_state"
redacted_status cleanup_env "$cleanup_env_state"
redacted_status deploy_env updated
redacted_status permissions verified
PROVISION_SUCCESS=1
