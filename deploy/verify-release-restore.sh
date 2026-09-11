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
for command in pg_restore psql tar sha256sum stat find sort diff python3 mktemp chmod rm; do require_command "$command"; done

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

# Verifier state must never share names or lifecycle with restored content.
# Create one private, invocation-owned workspace outside every protected tree.
scratch_parent="${RELEASE_VERIFY_TMPDIR:-/tmp}"
require_safe_absolute_path "$scratch_parent" backup "$checkout_root"
[[ -d "$scratch_parent" && ! -L "$scratch_parent" ]] || die "verify_temp_root_invalid"
scratch_parent_resolved="$(_canonical_path "$scratch_parent")"
for protected_root in "$generation_resolved" "$restore_resolved" "$production_resolved"; do
  case "$scratch_parent_resolved/" in "$protected_root"/*) die "verify_temp_root_protected" ;; esac
done
scratch="$(mktemp -d -- "$scratch_parent/eurith-release-verify.XXXXXXXX")" || die "verify_temp_create_failed"
chmod 0700 -- "$scratch" || die "verify_temp_mode_failed"
[[ -d "$scratch" && ! -L "$scratch" && "$(stat -c '%a' -- "$scratch")" == 700 ]] || die "verify_temp_invalid"
cleanup_scratch() { rm -rf -- "$scratch"; }
trap cleanup_scratch EXIT

dump="$generation/database.dump"
archive="$generation/releases.tar"
manifest="$generation/manifest.sha256"
for item in "$dump" "$archive" "$manifest"; do [[ -f "$item" && ! -L "$item" ]] || die "backup_component_invalid"; done

pg_env="$scratch/pg.env"
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

# Fail closed against the complete set of database-local object families that
# can make an otherwise disposable restore target non-pristine. PostgreSQL's
# built-in objects live in system schemas or below FirstNormalObjectId (16384);
# public and plpgsql are the only allowed initialized-database objects.
require_catalog_zero "WITH database_objects(object_kind, object_oid) AS (
  SELECT 'schema', n.oid FROM pg_catalog.pg_namespace n
    WHERE n.nspname NOT IN ('public','pg_catalog','information_schema','pg_toast')
      AND n.nspname !~ '^pg_(temp|toast_temp)_[0-9]+$'
  UNION ALL SELECT 'relation', c.oid FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' OR (n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND n.nspname !~ '^pg_(temp|toast_temp)_[0-9]+$')
  UNION ALL SELECT 'routine', p.oid FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' OR (n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND n.nspname !~ '^pg_(temp|toast_temp)_[0-9]+$')
  UNION ALL SELECT 'type', t.oid FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
    WHERE n.nspname='public' OR (n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND n.nspname !~ '^pg_(temp|toast_temp)_[0-9]+$')
  UNION ALL SELECT 'extension', e.oid FROM pg_catalog.pg_extension e WHERE e.extname <> 'plpgsql'
  UNION ALL SELECT 'operator', o.oid FROM pg_catalog.pg_operator o JOIN pg_catalog.pg_namespace n ON n.oid=o.oprnamespace WHERE n.nspname='public'
  UNION ALL SELECT 'collation', c.oid FROM pg_catalog.pg_collation c JOIN pg_catalog.pg_namespace n ON n.oid=c.collnamespace WHERE n.nspname='public'
  UNION ALL SELECT 'conversion', c.oid FROM pg_catalog.pg_conversion c JOIN pg_catalog.pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public'
  UNION ALL SELECT 'operator_class', o.oid FROM pg_catalog.pg_opclass o JOIN pg_catalog.pg_namespace n ON n.oid=o.opcnamespace WHERE n.nspname='public'
  UNION ALL SELECT 'operator_family', o.oid FROM pg_catalog.pg_opfamily o JOIN pg_catalog.pg_namespace n ON n.oid=o.opfnamespace WHERE n.nspname='public'
  UNION ALL SELECT 'text_search_parser', t.oid FROM pg_catalog.pg_ts_parser t JOIN pg_catalog.pg_namespace n ON n.oid=t.prsnamespace WHERE n.nspname='public'
  UNION ALL SELECT 'text_search_config', t.oid FROM pg_catalog.pg_ts_config t JOIN pg_catalog.pg_namespace n ON n.oid=t.cfgnamespace WHERE n.nspname='public'
  UNION ALL SELECT 'text_search_dictionary', t.oid FROM pg_catalog.pg_ts_dict t JOIN pg_catalog.pg_namespace n ON n.oid=t.dictnamespace WHERE n.nspname='public'
  UNION ALL SELECT 'text_search_template', t.oid FROM pg_catalog.pg_ts_template t JOIN pg_catalog.pg_namespace n ON n.oid=t.tmplnamespace WHERE n.nspname='public'
  UNION ALL SELECT 'foreign_data_wrapper', f.oid FROM pg_catalog.pg_foreign_data_wrapper f
  UNION ALL SELECT 'foreign_server', s.oid FROM pg_catalog.pg_foreign_server s
  UNION ALL SELECT 'user_mapping', u.oid FROM pg_catalog.pg_user_mapping u
  UNION ALL SELECT 'publication', p.oid FROM pg_catalog.pg_publication p
  UNION ALL SELECT 'large_object', l.oid FROM pg_catalog.pg_largeobject_metadata l
  UNION ALL SELECT 'event_trigger', e.oid FROM pg_catalog.pg_event_trigger e
  UNION ALL SELECT 'default_acl', d.oid FROM pg_catalog.pg_default_acl d
  UNION ALL SELECT 'extended_statistics', s.oid FROM pg_catalog.pg_statistic_ext s JOIN pg_catalog.pg_namespace n ON n.oid=s.stxnamespace WHERE n.nspname='public'
  UNION ALL SELECT 'language', l.oid FROM pg_catalog.pg_language l WHERE l.lanname NOT IN ('internal','c','sql','plpgsql')
  UNION ALL SELECT 'cast', c.oid FROM pg_catalog.pg_cast c WHERE c.oid >= 16384
  UNION ALL SELECT 'transform', t.oid FROM pg_catalog.pg_transform t WHERE t.oid >= 16384
  UNION ALL SELECT 'access_method', a.oid FROM pg_catalog.pg_am a WHERE a.oid >= 16384
  UNION ALL SELECT 'subscription', s.oid FROM pg_catalog.pg_subscription s WHERE s.subdbid=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
  UNION ALL SELECT 'database_setting', d.setdatabase FROM pg_catalog.pg_db_role_setting d WHERE d.setdatabase=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
  UNION ALL SELECT 'security_label', s.objoid FROM pg_catalog.pg_seclabel s
  UNION ALL SELECT 'database_security_label', s.objoid FROM pg_catalog.pg_shseclabel s WHERE s.classoid='pg_catalog.pg_database'::pg_catalog.regclass AND s.objoid=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
  UNION ALL SELECT 'public_schema_comment', d.objoid FROM pg_catalog.pg_description d WHERE d.classoid='pg_catalog.pg_namespace'::pg_catalog.regclass AND d.objoid='public'::pg_catalog.regnamespace AND d.description <> 'standard public schema'
  UNION ALL SELECT 'database_comment', d.objoid FROM pg_catalog.pg_shdescription d WHERE d.classoid='pg_catalog.pg_database'::pg_catalog.regclass AND d.objoid=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
)
SELECT count(*) FROM database_objects"
pg_restore --exit-on-error --no-owner --no-privileges --dbname "$PGDATABASE" "$dump" || die "database_restore_failed"
tar --extract --file "$archive" --directory "$restore_root" || die "archive_restore_failed"

rows="$scratch/registry.tsv"
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

expected_artifacts="$scratch/expected-artifacts.tsv"
actual_artifacts="$scratch/actual-artifacts.tsv"
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
unset PGPASSWORD PGUSER PGDATABASE PGPORT PGHOST
printf 'rows=%s\n' "$row_count"
printf 'files=%s\n' "$file_count"
printf 'manifest_path=%s\n' "$manifest"
printf 'manifest_sha256=%s\n' "$manifest_digest"
