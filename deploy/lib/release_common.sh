#!/usr/bin/env bash
set -Eeuo pipefail

die() { printf '%s\n' "error=${1:-operation_failed}" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "required_command_missing"; }
require_root() { [[ "${EUID:-$(id -u)}" == "0" ]] || die "root_required"; }
require_full_sha() { [[ "${1:-}" =~ ^[0-9a-f]{40}$ ]] || die "invalid_commit_sha"; }

_reject_newline() {
  local value="${1-}"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || die "invalid_multiline_input"
}

_canonical_path() { /usr/bin/readlink -m -- "$1" 2>/dev/null || die "invalid_path"; }

_reject_symlink_components() {
  local current="$1"
  while [[ "$current" != "/" && "$current" != "." ]]; do
    [[ ! -L "$current" ]] || die "protected_path_is_symlink"
    current="$(dirname -- "$current")"
  done
}

require_safe_absolute_path() {
  local value="${1:-}" purpose="${2:-mutable}" checkout="${3:-}" resolved checkout_resolved
  _reject_newline "$value"
  [[ "$value" == /* && "$value" != "/" ]] || die "unsafe_absolute_path"
  resolved="$(_canonical_path "$value")"
  [[ "$resolved" != "/" ]] || die "unsafe_absolute_path"
  _reject_symlink_components "$value"
  if [[ "$purpose" == "secret" || "$purpose" == "backup" ]]; then
    [[ -n "$checkout" ]] || die "checkout_required"
    checkout_resolved="$(_canonical_path "$checkout")"
    case "$resolved/" in "$checkout_resolved"/*) die "protected_path_below_checkout" ;; esac
  fi
}

assert_mode_owner_group() {
  local path="$1" expected_mode="$2" expected_owner="$3" expected_group="$4" actual
  [[ -e "$path" && ! -L "$path" ]] || die "protected_object_missing_or_symlink"
  actual="$(stat -c '%a:%u:%g' -- "$path" 2>/dev/null)" || die "protected_metadata_unreadable"
  [[ "$actual" == "${expected_mode}:${expected_owner}:${expected_group}" ]] || die "protected_metadata_mismatch"
}

assert_mount_vfs_options() {
  local target="$1" options
  options="$(findmnt -n -o VFS-OPTIONS --target "$target" 2>/dev/null)" || die "protected_mount_missing"
  case ",$options," in *,ro,*) ;; *) die "protected_mount_not_read_only" ;; esac
  case ",$options," in *,nosymfollow,*) ;; *) die "protected_mount_allows_symlink_follow" ;; esac
}

assert_regular_read_and_external_symlink_denied() {
  local regular="$1" external="$2" expected_target="${3:-/etc/passwd}"
  [[ -f "$regular" && ! -L "$regular" ]] || die "regular_probe_invalid"
  head -c 1 "$regular" >/dev/null 2>&1 || die "regular_probe_unreadable"
  [[ "$(readlink -- "$external" 2>/dev/null)" == "$expected_target" ]] || die "external_probe_invalid"
  if head -c 1 "$external" >/dev/null 2>&1; then die "external_symlink_followed"; fi
}

sha256_file() { sha256sum -- "$1" | awk '{print $1}'; }

release_asset_manifest_hash() {
  local root="$1" relative digest manifest='' aggregate
  for relative in deploy/compose.release.yml deploy/caddy/Caddyfile deploy/caddy/caddy-entrypoint.sh; do
    [[ -f "$root/$relative" && ! -L "$root/$relative" ]] || die runtime_asset_missing
    digest="$(sha256_file "$root/$relative")" || die runtime_asset_hash_unreadable
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die runtime_asset_hash_invalid
    manifest+="${digest}  ${relative}"$'\n'
  done
  aggregate="$(printf '%s' "$manifest" | sha256sum | awk '{print $1}')" || die runtime_asset_manifest_unreadable
  [[ "$aggregate" =~ ^[0-9a-f]{64}$ ]] || die runtime_asset_manifest_invalid
  printf '%s\n' "$aggregate"
}

redacted_status() {
  local key="$1" value="$2"
  [[ "$key" =~ ^[a-z_]+$ && "$value" =~ ^[a-z_]+$ ]] || die "invalid_status"
  printf '%s=%s\n' "$key" "$value"
}

verify_effective_boot_units() {
  local fragment="$1" drop_in="$2" unit output key value expected exec_pattern
  local -A properties=()
  exec_pattern='^\{ path=/usr/bin/env ; argv\[\]=/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C /usr/local/libexec/eurith-release-views ; ignore_errors=no ; start_time=\[[^]]*\] ; stop_time=\[[^]]*\] ; pid=[0-9]+ ; code=[^;{}]* ; status=[^;{}]* \}$'
  for unit in eurith-release-views.service docker.service; do
    properties=()
    output="$(systemctl show "$unit" --all --no-pager 2>/dev/null)" || die boot_systemd_inspection_failed
    while IFS='=' read -r key value; do
      [[ "$key" =~ ^[A-Za-z][A-Za-z0-9]*$ && ! -v properties[$key] ]] || die boot_systemd_properties_invalid
      properties[$key]="$value"
    done <<<"$output"
    for expected in LoadState=loaded Transient=no NeedDaemonReload=no; do
      key="${expected%%=*}"; value="${expected#*=}"
      [[ "${properties[$key]-missing}" == "$value" ]] || die boot_systemd_loaded_state_invalid
    done
    if [[ "$unit" == docker.service ]]; then
      case "${properties[FragmentPath]-}" in /usr/lib/systemd/system/docker.service|/lib/systemd/system/docker.service) ;; *) die boot_systemd_docker_fragment_invalid ;; esac
      [[ "${properties[DropInPaths]-}" == "$drop_in" ]] || die boot_systemd_docker_dropins_invalid
      for key in Requires After; do
        [[ " ${properties[$key]-} " == *' eurith-release-views.service '* ]] || die boot_systemd_docker_dependency_invalid
      done
      continue
    fi
    [[ "${properties[FragmentPath]-}" == "$fragment" ]] || die boot_systemd_release_fragment_invalid
    [[ "${properties[ExecStart]-}" =~ $exec_pattern ]] || die boot_systemd_release_exec_invalid
    [[ " ${properties[Before]-} " == *' docker.service '* ]] || die boot_systemd_release_dependency_invalid
    for value in /opt/eurith/releases /opt/eurith/release-caddy-probe-source; do
      [[ " ${properties[RequiresMountsFor]-} " == *" $value "* ]] || die boot_systemd_release_mount_dependency_invalid
    done
    for expected in Type=oneshot RemainAfterExit=yes PrivateMounts=no PrivateTmp=no ProtectSystem=no; do
      key="${expected%%=*}"; value="${expected#*=}"
      [[ "${properties[$key]-missing}" == "$value" ]] || die boot_systemd_release_execution_invalid
    done
    for key in SourcePath DropInPaths User Group Environment PassEnvironment RootDirectory RootImage WorkingDirectory BindPaths BindReadOnlyPaths TemporaryFileSystem InaccessiblePaths ReadOnlyPaths ReadWritePaths; do
      [[ -v properties[$key] && -z "${properties[$key]}" ]] || die boot_systemd_release_override_invalid
    done
    # systemctl's struct-array renderer omits these keys when empty, even
    # with --all. Nonempty arrays always emit a property and are rejected.
    for key in EnvironmentFiles ExecStartPre ExecStartPost ExecCondition ExecStop ExecStopPost; do
      [[ -z "${properties[$key]-}" ]] || die boot_systemd_release_override_invalid
    done
  done
}
