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

redacted_status() {
  local key="$1" value="$2"
  [[ "$key" =~ ^[a-z_]+$ && "$value" =~ ^[a-z_]+$ ]] || die "invalid_status"
  printf '%s=%s\n' "$key" "$value"
}
