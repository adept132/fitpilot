from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import quote, unquote, urlsplit
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "deploy" / "backup-release-state.sh"
RESTORE = ROOT / "deploy" / "verify-release-restore.sh"
BASH = shutil.which("bash") or "D:/Git/usr/bin/bash.exe"
SHA = "a" * 40
APK = b"EURITH-PAIRED-BACKUP"
APK_SHA = hashlib.sha256(APK).hexdigest()
KEY = f"android/sha256/{APK_SHA}.apk"


def _shell(path: Path) -> str:
    value = path.resolve().as_posix()
    if len(value) > 2 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


FAKE_DRIVER = r'''from __future__ import annotations
import hashlib, json, os, pathlib, shutil, subprocess, sys, tarfile

command, *args = sys.argv[1:]
sys.stdout.reconfigure(newline="\n")
state = pathlib.Path(os.environ["FAKE_STATE_DIR"])
state.mkdir(parents=True, exist_ok=True)
metadata_path = state / "metadata.json"
def metadata(): return json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
def save_metadata(value): metadata_path.write_text(json.dumps(value, sort_keys=True))
with (state / "calls.log").open("a", encoding="utf-8") as log:
    log.write(command + " " + " ".join(args) + "\n")

if command == "pg_dump":
    target = pathlib.Path(args[args.index("--file") + 1])
    target.write_bytes(b"PGDMP-paired-generation" if os.environ.get("FAKE_EMPTY_DUMP") != "1" else b"")
elif command == "pg_restore":
    source = pathlib.Path(args[-1])
    if not source.read_bytes().startswith(b"PGDMP"):
        sys.exit(3)
elif command == "psql":
    sql = args[args.index("--command") + 1]
    comprehensive_catalog_env = {
        "pg_catalog.pg_namespace": "FAKE_DB_SCHEMA_COUNT",
        "pg_catalog.pg_class": "FAKE_DB_RELATION_COUNT",
        "pg_catalog.pg_proc": "FAKE_DB_ROUTINE_COUNT",
        "pg_catalog.pg_type": "FAKE_DB_TYPE_COUNT",
        "pg_catalog.pg_extension": "FAKE_DB_EXTENSION_COUNT",
        "pg_catalog.pg_foreign_data_wrapper": "FAKE_DB_FDW_COUNT",
        "pg_catalog.pg_foreign_server": "FAKE_DB_SERVER_COUNT",
        "pg_catalog.pg_publication": "FAKE_DB_PUBLICATION_COUNT",
        "pg_catalog.pg_largeobject_metadata": "FAKE_DB_LARGE_OBJECT_COUNT",
        "pg_catalog.pg_collation": "FAKE_DB_COLLATION_COUNT",
        "pg_catalog.pg_conversion": "FAKE_DB_CONVERSION_COUNT",
        "pg_catalog.pg_ts_config": "FAKE_DB_TEXT_SEARCH_COUNT",
    }
    catalog_env = {
        "FROM pg_catalog.pg_namespace": "FAKE_DB_SCHEMA_COUNT",
        "FROM pg_catalog.pg_class": "FAKE_DB_RELATION_COUNT",
        "FROM pg_catalog.pg_proc": "FAKE_DB_ROUTINE_COUNT",
        "FROM pg_catalog.pg_type": "FAKE_DB_TYPE_COUNT",
        "FROM pg_catalog.pg_extension": "FAKE_DB_EXTENSION_COUNT",
    }
    if "database_objects" in sql:
        values = [os.environ.get(name, "0") for token, name in comprehensive_catalog_env.items() if token in sql]
        values.append(os.environ.get("FAKE_DB_OBJECT_COUNT", "0"))
        print(max(map(int, values)))
    else:
        matched = next((value for token, value in catalog_env.items() if token in sql), None)
        if matched:
            print(os.environ.get(matched, os.environ.get("FAKE_DB_OBJECT_COUNT", "0")))
        elif "artifact_storage_key" in sql:
            rows = pathlib.Path(os.environ["FAKE_ROWS_FILE"])
            if rows.exists(): print(rows.read_text(encoding="utf-8"), end="")
elif command == "tar":
    if os.environ.get("FAKE_TAR_FAIL") == "1": sys.exit(4)
    if "--create" in args:
        target = pathlib.Path(args[args.index("--file") + 1])
        root = pathlib.Path(args[args.index("--directory") + 1])
        with tarfile.open(target, "w") as archive:
            for item in sorted(root.rglob("*")):
                archive.add(item, arcname=item.relative_to(root).as_posix(), recursive=False)
            if os.environ.get("FAKE_ARCHIVE_UNSAFE") == "1":
                member = tarfile.TarInfo("unsafe-link")
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                archive.addfile(member)
    elif "--extract" in args:
        if os.environ.get("FAKE_TAR_EXTRACT_FAIL") == "1": sys.exit(5)
        source = pathlib.Path(args[args.index("--file") + 1])
        root = pathlib.Path(args[args.index("--directory") + 1])
        with tarfile.open(source) as archive:
            for member in archive.getmembers():
                # tarfile on Windows may preserve backslashes in names even
                # though the production GNU tar archive uses POSIX separators.
                target = root / pathlib.PurePosixPath(member.name.replace("\\", "/"))
                if member.isdir(): target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(member).read())
    elif "--list" in args:
        source = pathlib.Path(args[args.index("--file") + 1])
        with tarfile.open(source) as archive:
            for member in archive.getmembers(): print(member.name)
elif command == "sha256sum":
    if os.environ.get("FAKE_HASH_WRONG") == "1": print("0" * 64 + "  " + args[-1])
    else:
        path = pathlib.Path(args[-1]); print(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + str(path))
elif command == "mkdir":
    target = pathlib.Path(args[-1]); target.mkdir(parents="-p" in args, exist_ok="-p" in args); os.chmod(target, 0o700)
    data = metadata(); data[str(target.resolve())] = "700"; save_metadata(data)
elif command == "chmod":
    mode = int(args[0], 8)
    for target in args[1:]:
        if target != "--":
            os.chmod(target, mode)
            data = metadata(); data[str(pathlib.Path(target).resolve())] = oct(mode)[2:]; save_metadata(data)
elif command == "stat":
    path = pathlib.Path(args[-1])
    if "%a" in args: print(metadata().get(str(path.resolve()), oct(path.stat().st_mode & 0o777)[2:]))
    elif "%s" in args: print(path.stat().st_size)
    elif "%h" in args: print(path.stat().st_nlink)
    else: sys.exit(2)
else:
    sys.exit(127)
'''


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.volume = tmp_path / "source-volume"
        self.output = tmp_path / "backups"
        self.rows = tmp_path / "rows.tsv"
        self.db_file = tmp_path / "database-url"
        self.restore_db_file = tmp_path / "restore-database-url"
        self.checkout = tmp_path / "checkout"
        self.bin.mkdir(); self.state.mkdir(); self.volume.mkdir(); self.output.mkdir(); self.checkout.mkdir()
        driver = tmp_path / "fake_driver.py"
        driver.write_text(FAKE_DRIVER, encoding="utf-8")
        python = _shell(Path(sys.executable))
        for command in ("pg_dump", "pg_restore", "psql", "tar", "sha256sum", "mkdir", "chmod", "stat"):
            wrapper = self.bin / command
            wrapper.write_text(f'#!/usr/bin/env bash\nexec "{python}" "{_shell(driver)}" {command} "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
        python3 = self.bin / "python3"
        python3.write_text(f'#!/usr/bin/env bash\nexec "{python}" "$@"\n', encoding="utf-8")
        python3.chmod(0o755)
        artifact = self.volume / KEY
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(APK)
        (self.volume / ".staging").mkdir()
        (self.volume / ".staging" / "pending.part").write_bytes(b"staging")
        self.rows.write_text(f"{KEY}\tpublished\t{len(APK)}\t{APK_SHA}\n", encoding="utf-8")
        self.db_file.write_text("postgresql://backup-user:do-not-log@db.internal/eurith\n", encoding="utf-8")
        self.restore_db_file.write_text("postgresql://restore-user:do-not-log@localhost/eurith_restore_test\n", encoding="utf-8")

    def env(self, **extra: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "PATH": _shell(self.bin) + ":/usr/bin:/bin",
            "FAKE_STATE_DIR": _shell(self.state),
            "FAKE_ROWS_FILE": _shell(self.rows),
            "RELEASE_MUTATIONS_PAUSED": "1",
            "RELEASE_DATABASE_URL_FILE": _shell(self.db_file),
            "RELEASE_VOLUME_ROOT": _shell(self.volume),
            "RELEASE_CHECKOUT_ROOT": _shell(self.checkout),
            "RELEASE_BACKUP_NOW": "20260910T120000Z",
        })
        env.update(extra)
        return env

    def run_backup(self, *, env: dict[str, str] | None = None, output: Path | None = None, sha: str = SHA) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, _shell(BACKUP), sha, _shell(output or self.output)], cwd=ROOT,
            env=env or self.env(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )

    def generation(self) -> Path:
        children = list(self.output.iterdir())
        assert len(children) == 1
        return children[0]

    def run_restore(self, generation: Path | None = None, target: Path | None = None, db_file: Path | None = None, **extra: str) -> subprocess.CompletedProcess[str]:
        target = target or (self.tmp / "restore-volume")
        if target != self.volume:
            target.mkdir(exist_ok=True)
        return subprocess.run(
            [BASH, _shell(RESTORE), _shell(generation or self.generation()), _shell(db_file or self.restore_db_file), _shell(target)],
            cwd=ROOT, env=self.env(**extra), capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )


def test_backup_creates_one_private_complete_paired_generation(tmp_path: Path) -> None:
    """Catches publishing a partial generation or omitting staging data and artifact metadata."""
    harness = Harness(tmp_path)
    completed = harness.run_backup()

    assert completed.returncode == 0, completed.stderr
    generation = harness.generation()
    assert generation.name == f"20260910T120000Z-{SHA}"
    assert "mkdir -m 0700" in (harness.state / "calls.log").read_text()
    assert sorted(item.name for item in generation.iterdir()) == ["database.dump", "manifest.sha256", "releases.tar"]
    manifest = (generation / "manifest.sha256").read_text(encoding="utf-8")
    assert "format\t1\n" in manifest
    assert f"source_commit\t{SHA}\n" in manifest
    assert f"artifact\t{KEY}\t{len(APK)}\t{APK_SHA}\n" in manifest
    assert "database.dump" in manifest and "releases.tar" in manifest
    assert "do-not-log" not in completed.stdout + completed.stderr + (harness.state / "calls.log").read_text()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("pause", "mutations_not_paused"),
        ("sha", "invalid_commit_sha"),
        ("empty_dump", "database_dump_empty"),
        ("tar_fail", "archive_failed"),
        ("missing_apk", "published_artifact_missing"),
        ("hash", "artifact_hash_mismatch"),
        ("size", "artifact_size_mismatch"),
        ("escape", "invalid_storage_key"),
    ],
)
def test_backup_aborts_and_removes_only_incomplete_generation(tmp_path: Path, mutation: str, expected: str) -> None:
    """Catches a failed validation leaving a generation that deployment could mistake for durable."""
    harness = Harness(tmp_path)
    env = harness.env()
    sha = SHA
    if mutation == "pause": env.pop("RELEASE_MUTATIONS_PAUSED")
    elif mutation == "sha": sha = "abc"
    elif mutation == "empty_dump": env["FAKE_EMPTY_DUMP"] = "1"
    elif mutation == "tar_fail": env["FAKE_TAR_FAIL"] = "1"
    elif mutation == "missing_apk": (harness.volume / KEY).unlink()
    elif mutation == "hash": harness.rows.write_text(f"{KEY}\tpublished\t{len(APK)}\t{'0' * 64}\n")
    elif mutation == "size": harness.rows.write_text(f"{KEY}\tpublished\t{len(APK) + 1}\t{APK_SHA}\n")
    elif mutation == "escape": harness.rows.write_text(f"../outside.apk\tpublished\t{len(APK)}\t{APK_SHA}\n")

    completed = harness.run_backup(env=env, sha=sha)

    assert completed.returncode != 0
    assert expected in completed.stderr
    assert list(harness.output.iterdir()) == []


@pytest.mark.parametrize("location", ["checkout", "volume", "symlink"])
def test_backup_rejects_unsafe_output_locations(tmp_path: Path, location: str) -> None:
    """Catches backups being written into source-controlled, served, or symlink-redirected storage."""
    harness = Harness(tmp_path)
    if location == "checkout": output = harness.checkout / "backups"; output.mkdir()
    elif location == "volume": output = harness.volume / "backups"; output.mkdir()
    else:
        real = tmp_path / "real-output"; real.mkdir()
        output = tmp_path / "linked-output"
        try: output.symlink_to(real, target_is_directory=True)
        except OSError: pytest.skip("symlink creation is unavailable")

    completed = harness.run_backup(output=output)

    assert completed.returncode != 0
    assert "protected_path" in completed.stderr


def test_backup_rejects_hardlinks_anywhere_in_release_volume(tmp_path: Path) -> None:
    """Catches validating only final APK leaves while archiving aliased files elsewhere."""
    harness = Harness(tmp_path)
    source = harness.volume / ".staging" / "pending.part"
    alias = harness.volume / ".staging" / "pending.alias"
    try:
        os.link(source, alias)
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    completed = harness.run_backup()

    assert completed.returncode != 0
    assert "release_volume_hardlink_forbidden" in completed.stderr


def test_backup_rejects_symlink_anywhere_in_release_volume(tmp_path: Path) -> None:
    """Catches leaf-only checks that permit links in staging or nested metadata."""
    harness = Harness(tmp_path)
    link = harness.volume / ".staging" / "external"
    try:
        link.symlink_to(harness.rows)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    completed = harness.run_backup()

    assert completed.returncode != 0
    assert "release_volume_symlink_forbidden" in completed.stderr


def test_backup_rejects_special_files_anywhere_in_release_volume(tmp_path: Path) -> None:
    """Catches FIFOs/devices/sockets entering a supposedly complete regular-file archive."""
    harness = Harness(tmp_path)
    special = harness.volume / ".staging" / "unexpected.fifo"
    try:
        os.mkfifo(special)
    except (AttributeError, OSError):
        pytest.skip("special-file creation is unavailable")

    completed = harness.run_backup()

    assert completed.returncode != 0
    assert "release_volume_special_file_forbidden" in completed.stderr


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"FAKE_ARCHIVE_UNSAFE": "1"}, "unsafe_release_archive"),
        ({"FAKE_TAR_EXTRACT_FAIL": "1"}, "archive_restore_test_failed"),
    ],
)
def test_backup_inspects_and_test_restores_created_archive(
    tmp_path: Path, environment: dict[str, str], expected: str
) -> None:
    """Catches trusting a tar exit code without validating its entries and restorability."""
    harness = Harness(tmp_path)

    completed = harness.run_backup(env=harness.env(**environment))

    assert completed.returncode != 0
    assert expected in completed.stderr
    assert list(harness.output.iterdir()) == []


def test_restore_accepts_only_matching_pair_and_reports_redacted_counts(tmp_path: Path) -> None:
    """Catches restore verification skipping the database-to-file integrity join."""
    harness = Harness(tmp_path)
    assert harness.run_backup().returncode == 0

    completed = harness.run_restore()

    assert completed.returncode == 0, completed.stderr
    assert "rows=1" in completed.stdout and "files=1" in completed.stdout
    assert "manifest_sha256=" in completed.stdout
    assert "do-not-log" not in completed.stdout + completed.stderr + (harness.state / "calls.log").read_text()
    restore_call = next(
        line for line in (harness.state / "calls.log").read_text().splitlines()
        if line.startswith("pg_restore ")
    )
    assert "--dbname eurith_restore_test" in restore_call


def test_restore_preserves_scratch_named_files_from_archive_byte_for_byte(tmp_path: Path) -> None:
    """Catches verifier scratch state overwriting legitimate restored release-volume files."""
    harness = Harness(tmp_path)
    payloads = {
        ".registry.tsv": b"legitimate-registry-content\x00\xff",
        ".expected-artifacts.tsv": b"legitimate-expected-content\r\n",
        ".actual-artifacts.tsv": b"legitimate-actual-content\n",
    }
    for name, payload in payloads.items():
        (harness.volume / name).write_bytes(payload)
    assert harness.run_backup().returncode == 0
    restore_root = harness.tmp / "restore-volume"

    completed = harness.run_restore(target=restore_root)

    assert completed.returncode == 0, completed.stderr
    assert {name: (restore_root / name).read_bytes() for name in payloads} == payloads


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@db.internal/eurith_restore_test", "restore_database_not_local"),
        ("postgresql://u:p@localhost/eurith", "restore_database_name_invalid"),
        ("postgresql://u:p@localhost/eurith_restore_test?host=db.internal", "database_url_overrides_forbidden"),
        ("postgresql://u:p@localhost/eurith_restore_test#host=db.internal", "database_url_overrides_forbidden"),
    ],
)
def test_restore_rejects_nonlocal_nondisposable_or_overridden_database(tmp_path: Path, url: str, expected: str) -> None:
    """Catches URL routing tricks or a typo targeting a shared/production database."""
    harness = Harness(tmp_path)
    assert harness.run_backup().returncode == 0
    harness.restore_db_file.write_text(url + "\n")

    completed = harness.run_restore()

    assert completed.returncode != 0
    assert expected in completed.stderr


def test_restore_rejects_production_nonempty_or_symlink_volume(tmp_path: Path) -> None:
    """Catches restore extraction into production, existing data, or an indirect target."""
    harness = Harness(tmp_path)
    assert harness.run_backup().returncode == 0
    nonempty = tmp_path / "nonempty"; nonempty.mkdir(); (nonempty / "keep").write_text("x")
    production_child = harness.volume / "empty-restore-child"
    production_child.mkdir()
    linked = tmp_path / "linked"
    try: linked.symlink_to(nonempty, target_is_directory=True)
    except OSError: linked = None

    cases = [
        (harness.volume, "restore_volume_is_production"),
        (production_child, "restore_volume_is_production"),
        (nonempty, "restore_volume_not_empty"),
    ]
    if linked is not None: cases.append((linked, "protected_path_is_symlink"))
    for target, expected in cases:
        completed = harness.run_restore(target=target)
        assert completed.returncode != 0
        assert expected in completed.stderr


def test_restore_rejects_generation_stored_below_production_volume(tmp_path: Path) -> None:
    """Catches treating production-served data as a trusted backup generation."""
    harness = Harness(tmp_path)
    assert harness.run_backup().returncode == 0
    unsafe_generation = harness.volume / harness.generation().name
    shutil.copytree(harness.generation(), unsafe_generation)

    completed = harness.run_restore(generation=unsafe_generation)

    assert completed.returncode != 0
    assert "protected_path_below_release_volume" in completed.stderr


@pytest.mark.parametrize("mutation", ["dump", "archive", "manifest_generation", "missing", "hash", "size", "escape"])
def test_restore_fails_closed_on_mixed_or_corrupt_generation(tmp_path: Path, mutation: str) -> None:
    """Catches mix-and-match generations and restored registry/file divergence."""
    harness = Harness(tmp_path)
    assert harness.run_backup().returncode == 0
    generation = harness.generation()
    if mutation == "dump": (generation / "database.dump").write_bytes(b"PGDMP-other-generation")
    elif mutation == "archive": (generation / "releases.tar").write_bytes((generation / "releases.tar").read_bytes() + b"x")
    elif mutation == "manifest_generation":
        path = generation / "manifest.sha256"; path.write_text(path.read_text().replace(generation.name, "other-generation"))
    elif mutation == "missing": harness.rows.write_text(f"android/sha256/{'0'*64}.apk\tpublished\t1\t{'0'*64}\n")
    elif mutation == "hash": harness.rows.write_text(f"{KEY}\tpublished\t{len(APK)}\t{'0'*64}\n")
    elif mutation == "size": harness.rows.write_text(f"{KEY}\tpublished\t{len(APK)+1}\t{APK_SHA}\n")
    elif mutation == "escape": harness.rows.write_text(f"../outside.apk\tpublished\t1\t{'0'*64}\n")

    completed = harness.run_restore()

    assert completed.returncode != 0
    assert "do-not-log" not in completed.stdout + completed.stderr


def test_restore_rejects_nonempty_database_before_pg_restore(tmp_path: Path) -> None:
    """Catches destructive restore over an already-used disposable database."""
    harness = Harness(tmp_path)
    assert harness.run_backup().returncode == 0

    completed = harness.run_restore(FAKE_DB_OBJECT_COUNT="1")

    assert completed.returncode != 0
    assert "restore_database_not_empty" in completed.stderr
    assert "pg_restore" not in (harness.state / "calls.log").read_text().split("psql")[-1]


@pytest.mark.parametrize(
    ("environment", "catalog", "object_kind"),
    [
        ("FAKE_DB_SCHEMA_COUNT", "pg_catalog.pg_namespace", "other_schema"),
        ("FAKE_DB_RELATION_COUNT", "pg_catalog.pg_class", "table"),
        ("FAKE_DB_RELATION_COUNT", "pg_catalog.pg_class", "sequence"),
        ("FAKE_DB_RELATION_COUNT", "pg_catalog.pg_class", "view"),
        ("FAKE_DB_RELATION_COUNT", "pg_catalog.pg_class", "materialized_view"),
        ("FAKE_DB_RELATION_COUNT", "pg_catalog.pg_class", "foreign_table"),
        ("FAKE_DB_ROUTINE_COUNT", "pg_catalog.pg_proc", "function"),
        ("FAKE_DB_TYPE_COUNT", "pg_catalog.pg_type", "enum_or_type"),
        ("FAKE_DB_EXTENSION_COUNT", "pg_catalog.pg_extension", "extension"),
    ],
)
def test_restore_rejects_every_user_object_category_before_pg_restore(
    tmp_path: Path, environment: str, catalog: str, object_kind: str
) -> None:
    """Catches a nominally empty DB that contains schemas, routines, types or extensions."""
    harness = Harness(tmp_path)
    assert harness.run_backup().returncode == 0

    completed = harness.run_restore(**{environment: "1"})

    assert completed.returncode != 0
    assert "restore_database_not_empty" in completed.stderr
    calls = (harness.state / "calls.log").read_text()
    assert catalog in calls
    if object_kind in {"table", "sequence", "view", "materialized_view", "foreign_table"}:
        relation_probe = next(line for line in calls.splitlines() if "FROM pg_catalog.pg_class" in line)
        assert "relkind" not in relation_probe
    assert "pg_restore" not in calls


@pytest.mark.parametrize(
    ("environment", "catalog", "object_kind"),
    [
        ("FAKE_DB_FDW_COUNT", "pg_catalog.pg_foreign_data_wrapper", "foreign_data_wrapper"),
        ("FAKE_DB_SERVER_COUNT", "pg_catalog.pg_foreign_server", "foreign_server"),
        ("FAKE_DB_PUBLICATION_COUNT", "pg_catalog.pg_publication", "publication"),
        ("FAKE_DB_LARGE_OBJECT_COUNT", "pg_catalog.pg_largeobject_metadata", "large_object"),
        ("FAKE_DB_COLLATION_COUNT", "pg_catalog.pg_collation", "public_collation"),
        ("FAKE_DB_CONVERSION_COUNT", "pg_catalog.pg_conversion", "public_conversion"),
        ("FAKE_DB_TEXT_SEARCH_COUNT", "pg_catalog.pg_ts_config", "public_text_search_object"),
    ],
)
def test_restore_pristine_baseline_rejects_database_and_schema_level_objects(
    tmp_path: Path, environment: str, catalog: str, object_kind: str
) -> None:
    """Catches a selective emptiness gate that misses non-relation database objects."""
    harness = Harness(tmp_path)
    assert harness.run_backup().returncode == 0

    completed = harness.run_restore(**{environment: "1"})

    assert completed.returncode != 0, object_kind
    assert "restore_database_not_empty" in completed.stderr
    calls = (harness.state / "calls.log").read_text()
    assert catalog in calls
    assert "pg_restore" not in calls


def test_real_disposable_postgresql_paired_roundtrip(tmp_path: Path) -> None:
    """Opt-in proof using real PostgreSQL tools and two invocation-owned databases."""
    if os.environ.get("RUN_REAL_RELEASE_BACKUP_RESTORE") != "1":
        pytest.skip("set RUN_REAL_RELEASE_BACKUP_RESTORE=1 for the real recovery drill")
    raw_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not raw_url:
        pytest.fail("TEST_DATABASE_URL is required when the real recovery drill is enabled")
    parsed = urlsplit(raw_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.query
        or parsed.fragment
        or not parsed.path.strip("/")
    ):
        pytest.fail("TEST_DATABASE_URL must be a plain local PostgreSQL URL")

    tools = {name: shutil.which(name) for name in ("psql", "pg_dump", "pg_restore")}
    missing = [name for name, path in tools.items() if path is None]
    if missing:
        pytest.fail(f"required PostgreSQL tools are unavailable: {', '.join(missing)}")

    suffix = uuid.uuid4().hex[:12]
    source_database = f"eurith_backup_{suffix}"
    restore_database = f"eurith_restore_{suffix}"
    created: list[str] = []
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    pg_environment = os.environ.copy()
    pg_environment.update(
        {
            "PGHOST": parsed.hostname or "",
            "PGPORT": str(parsed.port or 5432),
            "PGUSER": username,
            "PGPASSWORD": password,
            "PGDATABASE": parsed.path.strip("/"),
            "PGCONNECT_TIMEOUT": "5",
        }
    )
    psql = str(tools["psql"])

    def sql(command: str, database: str | None = None) -> None:
        completed = subprocess.run(
            [psql, "--no-psqlrc", "--set", "ON_ERROR_STOP=1", "--dbname", database or pg_environment["PGDATABASE"], "--command", command],
            env=pg_environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, "real PostgreSQL setup operation failed"

    def database_url(database: str) -> str:
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        authority = host if parsed.port is None else f"{host}:{parsed.port}"
        credentials = quote(username, safe="")
        if password:
            credentials += f":{quote(password, safe='')}"
        return f"postgresql://{credentials}@{authority}/{database}"

    volume = tmp_path / "source-volume"
    output = tmp_path / "backups"
    restore_root = tmp_path / "restore-volume"
    wrappers = tmp_path / "posix-wrappers"
    for path in (volume / "android" / "sha256", volume / ".staging", output, restore_root):
        path.mkdir(parents=True, exist_ok=True)
    payload = b"EURITH-REAL-PAIRED-BACKUP"
    digest = hashlib.sha256(payload).hexdigest()
    key = f"android/sha256/{digest}.apk"
    (volume / key).write_bytes(payload)
    (volume / ".staging" / "pending.part").write_bytes(b"staging")
    source_url_file = tmp_path / "source.env"
    restore_url_file = tmp_path / "restore.env"
    source_url_file.write_text(f"DATABASE_URL={database_url(source_database)}\n", encoding="utf-8")
    restore_url_file.write_text(f"DATABASE_URL={database_url(restore_database)}\n", encoding="utf-8")
    if os.name != "nt":
        source_url_file.chmod(0o600)
        restore_url_file.chmod(0o600)

    script_environment = pg_environment.copy()
    script_environment.update(
        {
            "RELEASE_MUTATIONS_PAUSED": "1",
            "RELEASE_DATABASE_URL_FILE": _shell(source_url_file),
            "RELEASE_VOLUME_ROOT": _shell(volume),
            "RELEASE_CHECKOUT_ROOT": _shell(ROOT),
        }
    )

    def run_script(script: Path, *arguments: Path | str) -> subprocess.CompletedProcess[str]:
        command = [BASH, _shell(script), *(_shell(value) if isinstance(value, Path) else value for value in arguments)]
        environment = script_environment.copy()
        if os.name == "nt":
            wrappers.mkdir(exist_ok=True)
            wrapper_content = {
                "python3": f'#!/usr/bin/env bash\nexec "{_shell(Path(sys.executable))}" "$@"\n',
                "mkdir": '#!/usr/bin/env bash\nargs=(); while (($#)); do case "$1" in -m) shift 2;; --) shift;; *) args+=("$1"); shift;; esac; done; exec /usr/bin/mkdir "${args[@]}"\n',
                "stat": '#!/usr/bin/env bash\nif [[ "$*" == *"%a"* ]]; then printf "700\\n"; exit 0; fi; exec /usr/bin/stat "$@"\n',
                "chmod": "#!/usr/bin/env bash\nexit 0\n",
                "psql": f'#!/usr/bin/env bash\nset -o pipefail\n"{_shell(Path(psql))}" "$@" | tr -d "\\r"\n',
            }
            for name, content in wrapper_content.items():
                (wrappers / name).write_text(content, encoding="utf-8", newline="\n")
            pg_bin = _shell(Path(str(tools["psql"])).parent)
            prelude = 'export PATH="$1:$2:/usr/bin:/bin"; shift 2; exec "$@"'
            command = [BASH, "-lc", prelude, "bash", _shell(wrappers), pg_bin, *command[1:]]
        return subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60)

    try:
        sql(f'CREATE DATABASE "{source_database}"')
        created.append(source_database)
        sql(f'CREATE DATABASE "{restore_database}"')
        created.append(restore_database)
        ddl = (
            "CREATE TABLE app_releases (artifact_storage_key text, status text, "
            "artifact_size_bytes bigint, artifact_sha256 text, delivery_method text, "
            "artifact_deleted_at timestamptz);"
            f"INSERT INTO app_releases VALUES ('{key}','published',{len(payload)},'{digest}','direct_apk',NULL),"
            f"('{key}','withdrawn',{len(payload)},'{digest}','direct_apk',NULL);"
        )
        sql(ddl, source_database)

        backup = run_script(BACKUP, SHA, output)
        assert backup.returncode == 0, backup.stderr
        generations = list(output.iterdir())
        assert len(generations) == 1
        restore = run_script(RESTORE, generations[0], restore_url_file, restore_root)
        assert restore.returncode == 0, restore.stderr
        assert "rows=2" in restore.stdout and "files=1" in restore.stdout
        assert (restore_root / key).read_bytes() == payload
        manifest = (generations[0] / "manifest.sha256").read_text(encoding="utf-8")
        assert f"artifact\t{key}\t{len(payload)}\t{digest}\n" in manifest
        combined_output = backup.stdout + backup.stderr + restore.stdout + restore.stderr
        assert password not in combined_output if password else True
    finally:
        for database in reversed(created):
            sql(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        source_url_file.unlink(missing_ok=True)
        restore_url_file.unlink(missing_ok=True)
