from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

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
    if "pg_catalog.pg_class" in sql:
        print(os.environ.get("FAKE_DB_OBJECT_COUNT", "0"))
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
    elif "--extract" in args:
        source = pathlib.Path(args[args.index("--file") + 1])
        root = pathlib.Path(args[args.index("--directory") + 1])
        with tarfile.open(source) as archive: archive.extractall(root)
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
        if target != "--": os.chmod(target, mode)
elif command == "stat":
    path = pathlib.Path(args[-1])
    if "%a" in args: print(metadata().get(str(path.resolve()), oct(path.stat().st_mode & 0o777)[2:]))
    elif "%s" in args: print(path.stat().st_size)
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
