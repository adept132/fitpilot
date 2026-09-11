#!/usr/bin/env bash
set -Eeuo pipefail
set +x

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=deploy/lib/release_common.sh
source "$SCRIPT_DIR/lib/release_common.sh"

[[ $# == 2 ]] || die "usage"
source_sha="$1"
output_root="$2"
release_root="${RELEASE_VOLUME_ROOT:-/opt/eurith/releases}"
checkout_root="${RELEASE_CHECKOUT_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd -P)}"
database_url_file="${RELEASE_DATABASE_URL_FILE:-/etc/eurith/release-cleanup.env}"
generation=""
complete=0

cleanup_incomplete() {
  if [[ "$complete" != 1 && -n "$generation" && -d "$generation" ]]; then
    rm -rf -- "$generation"
  fi
}
trap cleanup_incomplete EXIT

[[ "${RELEASE_MUTATIONS_PAUSED:-}" == 1 ]] || die "mutations_not_paused"
require_full_sha "$source_sha"
for command in pg_dump psql tar sha256sum stat find sort python3; do require_command "$command"; done

require_safe_absolute_path "$output_root" backup "$checkout_root"
require_safe_absolute_path "$release_root" mutable
require_safe_absolute_path "$database_url_file" secret "$checkout_root"
[[ -d "$output_root" && ! -L "$output_root" ]] || die "backup_output_missing"
[[ -d "$release_root" && ! -L "$release_root" ]] || die "release_volume_missing"
output_resolved="$(_canonical_path "$output_root")"
release_resolved="$(_canonical_path "$release_root")"
case "$output_resolved/" in "$release_resolved"/*) die "protected_path_below_release_volume" ;; esac

started_at="${RELEASE_BACKUP_NOW:-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$started_at" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || die "invalid_backup_timestamp"
generation_name="${started_at}-${source_sha}"
generation="$output_root/$generation_name"
[[ ! -e "$generation" && ! -L "$generation" ]] || die "backup_generation_exists"
mkdir -m 0700 -- "$generation" || die "backup_generation_create_failed"
[[ "$(stat -c '%a' -- "$generation")" == 700 ]] || die "backup_generation_mode_invalid"

pg_env="$generation/.pg.env"
python3 - "$database_url_file" backup "$pg_env" <<'PY'
import os, pathlib, shlex, sys
from urllib.parse import unquote, urlsplit

source, mode, destination = map(pathlib.Path, (sys.argv[1], sys.argv[2], sys.argv[3]))
if not source.is_file() or source.is_symlink():
    raise SystemExit("error=database_url_file_invalid")
lines = [line.strip() for line in source.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
entries = {}
if len(lines) == 1 and "=" not in lines[0]:
    raw = lines[0]
else:
    for line in lines:
        if "=" not in line: raise SystemExit("error=database_url_file_invalid")
        key, value = line.split("=", 1)
        if key not in {"DATABASE_URL", "RELEASE_STORAGE_ROOT"} or key in entries: raise SystemExit("error=database_url_file_invalid")
        entries[key] = value
    raw = entries.get("DATABASE_URL", "")
if not raw: raise SystemExit("error=database_url_file_invalid")
parsed = urlsplit(raw)
if parsed.query or parsed.fragment:
    raise SystemExit("error=database_url_overrides_forbidden")
if parsed.scheme not in {"postgresql", "postgresql+asyncpg"} or not parsed.hostname:
    raise SystemExit("error=database_url_invalid")
database = unquote(parsed.path.lstrip("/"))
if not database or "/" in database:
    raise SystemExit("error=database_url_invalid")
values = {
    "PGHOST": parsed.hostname,
    "PGPORT": str(parsed.port or 5432),
    "PGDATABASE": database,
    "PGUSER": unquote(parsed.username or ""),
    "PGPASSWORD": unquote(parsed.password or ""),
}
if any(any(ord(char) < 32 for char in value) for value in values.values()):
    raise SystemExit("error=database_url_invalid")
destination.write_text("".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()), encoding="utf-8")
os.chmod(destination, 0o600)
PY
# The secret is loaded only into libpq environment variables; it is never argv or output.
unset PGHOSTADDR PGSERVICE PGSERVICEFILE PGOPTIONS PGAPPNAME
source "$pg_env"
rm -f -- "$pg_env"

rows_file="$generation/.registry.tsv"
psql --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align --field-separator=$'\t' \
  --command "SELECT artifact_storage_key, status, artifact_size_bytes, artifact_sha256 FROM app_releases WHERE delivery_method = 'direct_apk' AND artifact_deleted_at IS NULL ORDER BY artifact_storage_key" \
  >"$rows_file" || die "release_registry_query_failed"

validate_key() {
  local key="$1" expected_sha="$2"
  [[ "$key" =~ ^android/sha256/([0-9a-f]{64})\.apk$ ]] || die "invalid_storage_key"
  [[ "${BASH_REMATCH[1]}" == "$expected_sha" ]] || die "artifact_hash_mismatch"
}

while IFS=$'\t' read -r key status expected_size expected_sha extra; do
  [[ -n "$key" && -z "${extra:-}" && "$status" =~ ^(published|withdrawn)$ && "$expected_size" =~ ^[1-9][0-9]*$ && "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || die "release_registry_row_invalid"
  validate_key "$key" "$expected_sha"
  artifact="$release_root/$key"
  if [[ ! -f "$artifact" || -L "$artifact" ]]; then
    [[ "$status" != published ]] || die "published_artifact_missing"
    die "registry_artifact_missing"
  fi
  actual_size="$(stat -c '%s' -- "$artifact")" || die "artifact_stat_failed"
  [[ "$actual_size" == "$expected_size" ]] || die "artifact_size_mismatch"
  actual_sha="$(sha256_file "$artifact")"
  [[ "$actual_sha" == "$expected_sha" ]] || die "artifact_hash_mismatch"
done <"$rows_file"

dump="$generation/database.dump"
pg_dump --format=custom --file "$dump" || die "database_dump_failed"
[[ -s "$dump" && ! -L "$dump" ]] || die "database_dump_empty"

archive="$generation/releases.tar"
tar --create --file "$archive" --directory "$release_root" . || die "archive_failed"
[[ -s "$archive" && ! -L "$archive" ]] || die "archive_empty"

artifacts="$generation/.artifacts.tsv"
: >"$artifacts"
final_root="$release_root/android/sha256"
if [[ -d "$final_root" ]]; then
  if find -P "$final_root" -type l -print -quit | grep -q .; then die "final_artifact_symlink_forbidden"; fi
  while IFS= read -r -d '' artifact; do
    filename="${artifact##*/}"
    [[ "$filename" =~ ^[0-9a-f]{64}\.apk$ && -f "$artifact" && ! -L "$artifact" ]] || die "invalid_final_artifact"
    key="android/sha256/$filename"
    size="$(stat -c '%s' -- "$artifact")" || die "artifact_stat_failed"
    digest="$(sha256_file "$artifact")"
    [[ "$filename" == "$digest.apk" ]] || die "artifact_hash_mismatch"
    printf 'artifact\t%s\t%s\t%s\n' "$key" "$size" "$digest" >>"$artifacts"
  done < <(find -P "$final_root" -maxdepth 1 -type f -name '*.apk' -print0)
fi
LC_ALL=C sort -o "$artifacts" "$artifacts"

completed_at="${RELEASE_BACKUP_NOW:-$(date -u +%Y%m%dT%H%M%SZ)}"
dump_size="$(stat -c '%s' -- "$dump")"
archive_size="$(stat -c '%s' -- "$archive")"
dump_sha="$(sha256_file "$dump")"
archive_sha="$(sha256_file "$archive")"
manifest="$generation/manifest.sha256"
{
  printf 'format\t1\n'
  printf 'generation\t%s\n' "$generation_name"
  printf 'started_at\t%s\n' "$started_at"
  printf 'completed_at\t%s\n' "$completed_at"
  printf 'source_commit\t%s\n' "$source_sha"
  printf 'file\tdatabase.dump\t%s\t%s\n' "$dump_size" "$dump_sha"
  printf 'file\treleases.tar\t%s\t%s\n' "$archive_size" "$archive_sha"
  cat "$artifacts"
} >"$manifest"
chmod 0600 -- "$dump" "$archive" "$manifest"
rm -f -- "$rows_file" "$artifacts"
unset PGPASSWORD PGUSER PGDATABASE PGPORT PGHOST
complete=1
trap - EXIT
printf 'generation=%s\n' "$generation_name"
printf 'manifest_sha256=%s\n' "$(sha256_file "$manifest")"
