#!/usr/bin/env bash
set -Eeuo pipefail
set +x

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=deploy/lib/release_common.sh
source "$SCRIPT_DIR/lib/release_common.sh"

[[ $# == 3 ]] || die "usage"
generation="$1"
database_url_file="$2"
restore_root="$3"
checkout_root="${RELEASE_CHECKOUT_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd -P)}"
production_root="${RELEASE_VOLUME_ROOT:-/opt/eurith/releases}"
for command in pg_restore psql tar sha256sum stat find sort diff python3; do require_command "$command"; done

require_safe_absolute_path "$generation" backup "$checkout_root"
require_safe_absolute_path "$database_url_file" secret "$checkout_root"
require_safe_absolute_path "$restore_root" backup "$checkout_root"
[[ -d "$generation" && ! -L "$generation" ]] || die "backup_generation_invalid"
generation_resolved="$(_canonical_path "$generation")"
restore_resolved="$(_canonical_path "$restore_root")"
production_resolved="$(_canonical_path "$production_root")"
case "$generation_resolved/" in "$production_resolved"/*) die "protected_path_below_release_volume" ;; esac
case "$restore_resolved/" in "$production_resolved"/*) die "restore_volume_is_production" ;; esac
[[ -d "$restore_root" && ! -L "$restore_root" ]] || die "restore_volume_invalid"
[[ -z "$(find -P "$restore_root" -mindepth 1 -print -quit)" ]] || die "restore_volume_not_empty"

dump="$generation/database.dump"
archive="$generation/releases.tar"
manifest="$generation/manifest.sha256"
for item in "$dump" "$archive" "$manifest"; do [[ -f "$item" && ! -L "$item" ]] || die "backup_component_invalid"; done

pg_env="$restore_root/.pg.env"
python3 - "$database_url_file" restore "$pg_env" <<'PY'
import os, pathlib, re, shlex, sys
from urllib.parse import unquote, urlsplit

source = pathlib.Path(sys.argv[1]); destination = pathlib.Path(sys.argv[3])
if not source.is_file() or source.is_symlink(): raise SystemExit("error=database_url_file_invalid")
lines = [line.strip() for line in source.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
if len(lines) != 1: raise SystemExit("error=database_url_file_invalid")
raw = lines[0].split("=", 1)[1] if lines[0].startswith("DATABASE_URL=") else lines[0]
parsed = urlsplit(raw)
if parsed.query or parsed.fragment: raise SystemExit("error=database_url_overrides_forbidden")
if parsed.scheme not in {"postgresql", "postgresql+asyncpg"} or not parsed.hostname: raise SystemExit("error=database_url_invalid")
host = parsed.hostname; database = unquote(parsed.path.lstrip("/"))
if host not in {"localhost", "127.0.0.1", "::1"}: raise SystemExit("error=restore_database_not_local")
if re.fullmatch(r"eurith_restore_[a-z0-9][a-z0-9_]*", database) is None: raise SystemExit("error=restore_database_name_invalid")
values = {"PGHOST": host, "PGPORT": str(parsed.port or 5432), "PGDATABASE": database, "PGUSER": unquote(parsed.username or ""), "PGPASSWORD": unquote(parsed.password or "")}
if any(any(ord(char) < 32 for char in value) for value in values.values()): raise SystemExit("error=database_url_invalid")
destination.write_text("".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()), encoding="utf-8")
os.chmod(destination, 0o600)
PY
unset PGHOSTADDR PGSERVICE PGSERVICEFILE PGOPTIONS PGAPPNAME
source "$pg_env"
rm -f -- "$pg_env"

generation_name="${generation##*/}"
manifest_generation="$(awk -F '\t' '$1 == "generation" {print $2}' "$manifest")"
[[ "$manifest_generation" == "$generation_name" ]] || die "manifest_generation_mismatch"
[[ "$(awk -F '\t' '$1 == "format" {print $2}' "$manifest")" == 1 ]] || die "manifest_format_invalid"
[[ "$(awk -F '\t' '$1 == "source_commit" {print $2}' "$manifest")" =~ ^[0-9a-f]{40}$ ]] || die "manifest_source_commit_invalid"

verify_component() {
  local name="$1" path="$2" lines size expected_size digest expected_digest
  lines="$(awk -F '\t' -v name="$name" '$1 == "file" && $2 == name {print $3 "\t" $4}' "$manifest")"
  [[ "$(printf '%s\n' "$lines" | grep -c .)" == 1 ]] || die "manifest_component_invalid"
  IFS=$'\t' read -r expected_size expected_digest <<<"$lines"
  [[ "$expected_size" =~ ^[1-9][0-9]*$ && "$expected_digest" =~ ^[0-9a-f]{64}$ ]] || die "manifest_component_invalid"
  size="$(stat -c '%s' -- "$path")"; [[ "$size" == "$expected_size" ]] || die "backup_component_size_mismatch"
  digest="$(sha256_file "$path")"; [[ "$digest" == "$expected_digest" ]] || die "backup_component_hash_mismatch"
}
verify_component database.dump "$dump"
verify_component releases.tar "$archive"

# Reject traversal, links and special files before extraction; the complete archive may contain only dirs/regular files.
python3 - "$archive" <<'PY'
import pathlib, sys, tarfile
archive = pathlib.Path(sys.argv[1])
with tarfile.open(archive) as opened:
    seen = set()
    for member in opened.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
            raise SystemExit("error=unsafe_release_archive")
        normalized = path.as_posix()
        if normalized in seen:
            raise SystemExit("error=unsafe_release_archive")
        seen.add(normalized)
PY

require_catalog_zero() {
  local query="$1" count
  count="$(psql --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align --command "$query")" || die "restore_database_probe_failed"
  count="${count//[[:space:]]/}"
  [[ "$count" =~ ^[0-9]+$ ]] || die "restore_database_probe_invalid"
  [[ "$count" == 0 ]] || die "restore_database_not_empty"
}

# Explicit pristine-database baseline: public plus PostgreSQL system schemas,
# and optionally the default plpgsql extension. Any user schema/object fails.
require_catalog_zero "SELECT count(*) FROM pg_catalog.pg_namespace n WHERE n.nspname NOT IN ('public','pg_catalog','information_schema','pg_toast') AND n.nspname !~ '^pg_(temp|toast_temp)_[0-9]+$'"
require_catalog_zero "SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' OR (n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND n.nspname !~ '^pg_(temp|toast_temp)_[0-9]+$')"
require_catalog_zero "SELECT count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' OR (n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND n.nspname !~ '^pg_(temp|toast_temp)_[0-9]+$')"
require_catalog_zero "SELECT count(*) FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public' OR (n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND n.nspname !~ '^pg_(temp|toast_temp)_[0-9]+$')"
require_catalog_zero "SELECT count(*) FROM pg_catalog.pg_extension e WHERE e.extname NOT IN ('plpgsql')"
pg_restore --exit-on-error --no-owner --no-privileges --dbname "$PGDATABASE" "$dump" || die "database_restore_failed"
tar --extract --file "$archive" --directory "$restore_root" || die "archive_restore_failed"

rows="$restore_root/.registry.tsv"
psql --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align --field-separator=$'\t' \
  --command "SELECT artifact_storage_key, status, artifact_size_bytes, artifact_sha256 FROM app_releases WHERE delivery_method = 'direct_apk' AND artifact_deleted_at IS NULL ORDER BY artifact_storage_key" \
  >"$rows" || die "release_registry_query_failed"

row_count=0
while IFS=$'\t' read -r key status expected_size expected_sha extra; do
  [[ -n "$key" && -z "${extra:-}" && "$status" =~ ^(published|withdrawn)$ && "$expected_size" =~ ^[1-9][0-9]*$ && "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || die "release_registry_row_invalid"
  [[ "$key" =~ ^android/sha256/([0-9a-f]{64})\.apk$ && "${BASH_REMATCH[1]}" == "$expected_sha" ]] || die "invalid_storage_key"
  artifact="$restore_root/$key"
  if [[ ! -f "$artifact" || -L "$artifact" ]]; then
    [[ "$status" != published ]] || die "published_artifact_missing"
    die "registry_artifact_missing"
  fi
  [[ "$(stat -c '%s' -- "$artifact")" == "$expected_size" ]] || die "artifact_size_mismatch"
  [[ "$(sha256_file "$artifact")" == "$expected_sha" ]] || die "artifact_hash_mismatch"
  row_count=$((row_count + 1))
done <"$rows"

expected_artifacts="$restore_root/.expected-artifacts.tsv"
actual_artifacts="$restore_root/.actual-artifacts.tsv"
awk -F '\t' '$1 == "artifact" {print $0}' "$manifest" | LC_ALL=C sort >"$expected_artifacts"
: >"$actual_artifacts"
final_root="$restore_root/android/sha256"
if [[ -d "$final_root" ]]; then
  while IFS= read -r -d '' artifact; do
    [[ -f "$artifact" && ! -L "$artifact" ]] || die "invalid_final_artifact"
    filename="${artifact##*/}"; [[ "$filename" =~ ^[0-9a-f]{64}\.apk$ ]] || die "invalid_final_artifact"
    digest="$(sha256_file "$artifact")"; [[ "$filename" == "$digest.apk" ]] || die "artifact_hash_mismatch"
    printf 'artifact\tandroid/sha256/%s\t%s\t%s\n' "$filename" "$(stat -c '%s' -- "$artifact")" "$digest" >>"$actual_artifacts"
  done < <(find -P "$final_root" -maxdepth 1 -type f -name '*.apk' -print0)
fi
LC_ALL=C sort -o "$actual_artifacts" "$actual_artifacts"
diff -u -- "$expected_artifacts" "$actual_artifacts" >/dev/null || die "manifest_artifact_mismatch"
file_count="$(grep -c '^artifact' "$actual_artifacts" || true)"
manifest_digest="$(sha256_file "$manifest")"
rm -f -- "$rows" "$expected_artifacts" "$actual_artifacts"
unset PGPASSWORD PGUSER PGDATABASE PGPORT PGHOST
printf 'rows=%s\n' "$row_count"
printf 'files=%s\n' "$file_count"
printf 'manifest_path=%s\n' "$manifest"
printf 'manifest_sha256=%s\n' "$manifest_digest"
