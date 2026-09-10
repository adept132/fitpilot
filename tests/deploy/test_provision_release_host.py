from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "provision-release-host.sh"
LIBRARY = ROOT / "deploy" / "lib" / "release_common.sh"
BASH = shutil.which("bash") or "D:/Git/usr/bin/bash.exe"
TOKENS = {
    "publisher-token": "1" * 64,
    "operator-token": "2" * 64,
    "webhook-secret": "3" * 64,
}
DATABASE_URL = "postgresql+asyncpg://release-cleanup:never-log-this@db/eurith"


def _shell(path: Path) -> str:
    value = path.resolve().as_posix()
    if len(value) > 2 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


FAKE_DRIVER = r'''from __future__ import annotations
import json, os, pathlib, shutil, stat, sys, tempfile

command, *args = sys.argv[1:]
sys.stdout.reconfigure(newline="\n")
state_dir = pathlib.Path(os.environ["FAKE_STATE_DIR"])
state_dir.mkdir(parents=True, exist_ok=True)
metadata_path = state_dir / "metadata.json"
mounts_path = state_dir / "mounts.json"

def load(path):
    return json.loads(path.read_text()) if path.exists() else {}
def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True))
def key(value):
    return str(pathlib.Path(value).resolve())
def shell_path(value):
    value = str(value).replace("\\", "/")
    if len(value) > 2 and value[1] == ":": return "/" + value[0].lower() + value[2:]
    return value
def metadata_set(path, mode, owner, group):
    data = load(metadata_path)
    data[key(path)] = {"mode": str(mode).lstrip("0") or "0", "owner": str(owner).replace("root", "0"), "group": str(group).replace("root", "0")}
    save(metadata_path, data)

with (state_dir / "calls.log").open("a", encoding="utf-8") as log:
    log.write(command + " " + " ".join(args) + "\n")

if command == "install":
    mode, owner, group, directory = "755", "0", "0", False
    operands = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == "-d": directory = True; index += 1
        elif value in ("-m", "-o", "-g"):
            selected = args[index + 1]
            if value == "-m": mode = selected
            elif value == "-o": owner = selected
            else: group = selected
            index += 2
        elif value.startswith("-"): index += 1
        else: operands.append(value); index += 1
    if directory:
        for target in operands:
            pathlib.Path(target).mkdir(parents=True, exist_ok=True)
            os.chmod(target, int(mode, 8)); metadata_set(target, mode, owner, group)
    else:
        source, target = operands[-2:]
        pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target); os.chmod(target, int(mode, 8)); metadata_set(target, mode, owner, group)
elif command == "chown":
    owner_group, *paths = [a for a in args if not a.startswith("-")]
    owner, group = owner_group.split(":", 1)
    data = load(metadata_path)
    for path in paths:
        current = data.get(key(path), {"mode": "755"})
        metadata_set(path, current["mode"], owner, group)
elif command == "chmod":
    operands = [a for a in args if not a.startswith("-")]
    mode, *paths = operands
    data = load(metadata_path)
    for path in paths:
        current = data.get(key(path), {"owner": "0", "group": "0"})
        os.chmod(path, int(mode, 8)); metadata_set(path, mode, current["owner"], current["group"])
elif command == "mktemp":
    if os.environ.get("FAKE_MKTEMP_FAIL") == "1": sys.exit(1)
    parent_arg = next(a for a in args if a.startswith("--tmpdir="))
    parent = pathlib.Path(parent_arg.split("=", 1)[1])
    template = args[-1]; prefix = template.split("X", 1)[0]
    descriptor, created = tempfile.mkstemp(prefix=prefix, dir=parent)
    os.close(descriptor); metadata_set(created, "600", "0", "0"); print(shell_path(created))
elif command == "mv":
    operands = [a for a in args if not a.startswith("-")]
    source, target = operands[-2:]
    pathlib.Path(source).replace(target)
    data = load(metadata_path)
    record = data.pop(key(source), None)
    if record is not None: data[key(target)] = record
    save(metadata_path, data)
elif command == "stat":
    target = args[-1]
    target_key = key(target)
    mount_record = load(mounts_path).get(target_key)
    metadata_key = mount_record["source"] if mount_record and mount_record["source"] != target_key else target_key
    record = load(metadata_path).get(metadata_key)
    if record is None: sys.exit(1)
    if "%d:%i" in args: print(target_key if mount_record is None else mount_record["source"])
    else: print(f'{record["mode"]}:{record["owner"]}:{record["group"]}')
elif command == "getent":
    if (state_dir / "group-created").exists(): print("eurith-releases:x:4321:")
    else: sys.exit(2)
elif command == "groupadd":
    (state_dir / "group-created").write_text("yes")
elif command == "mount":
    mounts = load(mounts_path)
    call_kind = "bind" if "--bind" in args else "remount"
    target_name = pathlib.Path(args[-1]).name
    failure = os.environ.get("FAKE_MOUNT_FAIL_AT", "")
    if failure == f"{call_kind}-final" and target_name == "sha256": sys.exit(1)
    if failure == f"{call_kind}-probe" and target_name == ".probe": sys.exit(1)
    if "--bind" in args:
        source, target = args[-2:]
        mounts[key(target)] = {"source": key(source), "options": "rw"}
        if pathlib.Path(target).name == ".probe" and key(source) != key(target):
            source_path, target_path = pathlib.Path(source), pathlib.Path(target)
            metadata = load(metadata_path)
            for child in source_path.iterdir():
                copied = target_path / child.name
                shutil.copyfile(child, copied)
                if key(child) in metadata: metadata[key(copied)] = metadata[key(child)]
            save(metadata_path, metadata)
    else:
        target = args[-1]
        if key(target) not in mounts: sys.exit(1)
        mounts[key(target)]["options"] = "ro,nosymfollow"
    save(mounts_path, mounts)
elif command == "findmnt":
    target = args[-1]
    record = load(mounts_path).get(key(target))
    if record is None: sys.exit(1)
    if "VFS-OPTIONS" in args: print(record["options"])
    elif "TARGET" in args: print(shell_path(key(target)))
    elif "SOURCE" in args: print(shell_path(record["source"]))
    else: sys.exit(2)
elif command == "umount":
    target = pathlib.Path(args[-1]); mounts = load(mounts_path); record = mounts.pop(key(target), None); save(mounts_path, mounts)
    if target.name == ".probe" and record and record["source"] != key(target):
        for child in target.iterdir():
            if child.is_file(): child.unlink()
elif command == "ln":
    target, link = args[-2:]
    if target.replace("\\", "/").endswith("/etc/passwd"): target = "/etc/passwd"
    pathlib.Path(link).write_text("FAKE-SYMLINK:" + target)
    metadata_set(link, "777", "0", "0")
elif command == "readlink":
    value = pathlib.Path(args[-1]).read_text()
    if not value.startswith("FAKE-SYMLINK:"): sys.exit(1)
    print(value.removeprefix("FAKE-SYMLINK:"))
elif command == "head":
    target = pathlib.Path(args[-1])
    if target.name == "external": sys.exit(0 if os.environ.get("FAKE_HOST_FOLLOWS_SYMLINK") == "1" else 1)
    if target.is_file(): target.open("rb").read(1); sys.exit(0)
    sys.exit(1)
elif command == "find":
    directory = pathlib.Path(args[0])
    for child in sorted(directory.iterdir(), key=lambda p: p.name):
        kind = "d" if child.is_dir() else "f"
        if child.is_file() and child.read_bytes().startswith(b"FAKE-SYMLINK:"): kind = "l"
        print(f"{child.name}|{kind}")
elif command == "docker":
    joined = " ".join(args)
    if "--entrypoint id api -u" in joined:
        print("1234"); sys.exit(0)
    passed_env = {args[i + 1].split("=", 1)[0]: args[i + 1].split("=", 1)[1] for i, value in enumerate(args[:-1]) if value == "-e" and "=" in args[i + 1]}
    action = passed_env.get("EURITH_PROBE_ACTION", "")
    if os.environ.get("FAKE_DOCKER_FAIL_ACTION") == action: sys.exit(9)
    final_dir = pathlib.Path(os.environ["FAKE_FINAL_DIR"])
    staging_dir = pathlib.Path(os.environ["FAKE_STAGING_DIR"])
    fixture_name = passed_env.get("EURITH_PROBE_NAME", ".eurith-permission-probe-test")
    fixture_token = passed_env.get("EURITH_PROBE_TOKEN", "test-token")
    fixture = final_dir / fixture_name
    if action == "api-stage":
        if "eurith-provision" not in joined or "chmod 0600" not in joined or "mv -T -n" not in joined or "chmod 0640" not in joined: sys.exit(3)
        if fixture.exists(): sys.exit(4)
        staging = staging_dir / (fixture_name + ".tmp")
        staging.write_bytes(("eurith-provision:" + fixture_token).encode()); os.chmod(staging, 0o600)
        fixture.parent.mkdir(parents=True, exist_ok=True); staging.replace(fixture); os.chmod(fixture, 0o640)
        sys.exit(0)
    if action == "api-remove":
        expected = ("eurith-provision:" + fixture_token).encode()
        if fixture.exists() and fixture.read_bytes() == expected: fixture.unlink()
        sys.exit(0)
    if action == "container-gate":
        mounts = load(mounts_path)
        final_view = key(os.environ["FAKE_FINAL_VIEW"]); probe_view = key(os.environ["FAKE_PROBE_VIEW"])
        valid = all(mounts.get(path, {}).get("options") == "ro,nosymfollow" for path in (final_view, probe_view))
        valid = valid and os.environ.get("FAKE_CONTAINER_FOLLOWS_SYMLINK") != "1"
        sys.exit(0 if valid else 1)
    if action == "caddy-read": sys.exit(0 if fixture.is_file() and fixture.read_bytes() == ("eurith-provision:" + fixture_token).encode() else 1)
    if action.startswith("caddy-"):
        allowed = os.environ.get("FAKE_CADDY_MUTATION_SUCCEEDS", "")
        sys.exit(0 if allowed in ("all", action.removeprefix("caddy-")) else 1)
    sys.exit(2)
else:
    sys.exit(127)
'''


def _make_fake_commands(bin_dir: Path, state_dir: Path) -> None:
    driver = state_dir / "driver.py"
    driver.write_text(FAKE_DRIVER, encoding="utf-8")
    for command in ("docker", "findmnt", "mount", "umount", "getent", "groupadd", "install", "chown", "chmod", "stat", "ln", "readlink", "head", "mv", "mktemp", "find"):
        wrapper = bin_dir / command
        wrapper.write_text(
            f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{driver.as_posix()}" {command} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)


class Host:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        self.secrets = tmp_path / "secret-source"
        self.etc = tmp_path / "etc" / "eurith"
        self.state = tmp_path / "fake-state"
        self.bin = tmp_path / "fake-bin"
        self.base_compose = tmp_path / "compose.yml"
        self.root.mkdir(); self.secrets.mkdir(); self.etc.mkdir(parents=True); self.state.mkdir(); self.bin.mkdir()
        self.base_compose.write_text("services: {api: {}}\n", encoding="utf-8")
        for name, value in TOKENS.items():
            (self.secrets / name).write_bytes((value + "\n").encode("ascii"))
        (self.secrets / "cleanup-database-url").write_bytes((DATABASE_URL + "\n").encode("utf-8"))
        metadata = {str(path.resolve()): {"mode": "400", "owner": "0", "group": "0"} for path in self.secrets.iterdir()}
        metadata[str(self.secrets.resolve())] = {"mode": "700", "owner": "0", "group": "0"}
        metadata[str(self.etc.resolve())] = {"mode": "750", "owner": "0", "group": "4321"}
        (self.state / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        _make_fake_commands(self.bin, self.state)

    @property
    def api_env(self) -> Path: return self.etc / "api-release.env"
    @property
    def cleanup_env(self) -> Path: return self.etc / "release-cleanup.env"
    @property
    def deploy_env(self) -> Path: return self.etc / "release-deploy.env"
    @property
    def storage(self) -> Path: return self.root / "opt" / "eurith" / "releases"
    @property
    def final_view(self) -> Path: return self.root / "opt" / "eurith" / "release-caddy-view" / "android" / "sha256"
    @property
    def probe_view(self) -> Path: return self.root / "opt" / "eurith" / "release-caddy-view" / ".probe"
    @property
    def probe_source(self) -> Path: return self.root / "opt" / "eurith" / "release-caddy-probe-source"

    def run(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "PATH": _shell(self.bin) + ":/usr/bin:/bin",
            "FAKE_STATE_DIR": str(self.state),
            "FAKE_FINAL_DIR": str(self.storage / "android" / "sha256"),
            "FAKE_STAGING_DIR": str(self.storage / ".staging"),
            "FAKE_FINAL_VIEW": str(self.final_view),
            "FAKE_PROBE_VIEW": str(self.probe_view),
            "EURITH_FAKE_SYMLINKS": "1",
        })
        env.update(extra_env)
        args = [
            BASH, _shell(SCRIPT), "--root", _shell(self.root),
            "--secret-source-dir", _shell(self.secrets),
            "--api-release-env", _shell(self.api_env),
            "--cleanup-env", _shell(self.cleanup_env),
            "--deploy-env", _shell(self.deploy_env),
            "--base-compose", _shell(self.base_compose),
            "--release-overlay", _shell(ROOT / "deploy" / "compose.release.yml"),
            "--site-address", "https://api.eurith.app",
        ]
        return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, check=False)


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def _assert_redacted(result: subprocess.CompletedProcess[str]) -> None:
    combined = result.stdout + result.stderr
    for secret in (*TOKENS.values(), DATABASE_URL):
        assert secret not in combined


def test_scripts_exist() -> None:
    assert LIBRARY.is_file()
    assert SCRIPT.is_file()


def test_first_use_and_repeat_are_idempotent_and_redacted(host: Host) -> None:
    first = host.run()
    assert first.returncode == 0, first.stderr
    assert first.stdout.splitlines() == [
        "group=created", "storage=created", "release_view=created",
        "api_env=created", "cleanup_env=created", "deploy_env=updated",
        "permissions=verified",
    ]
    _assert_redacted(first)
    second = host.run()
    assert second.returncode == 0, second.stderr
    assert second.stdout.splitlines() == [
        "group=existing", "storage=verified", "release_view=verified",
        "api_env=existing", "cleanup_env=existing", "deploy_env=updated",
        "permissions=verified",
    ]
    _assert_redacted(second)
    assert host.deploy_env.read_text() == "RELEASE_SHARED_GID=4321\nEURITH_SITE_ADDRESS=https://api.eurith.app\n"
    assert not list((host.storage / ".staging").iterdir())
    assert not list((host.storage / "android" / "sha256").iterdir())


def test_env_files_have_exact_keys_and_storage_roots(host: Host) -> None:
    result = host.run()
    assert result.returncode == 0, result.stderr
    api_lines = host.api_env.read_text().splitlines()
    assert [line.split("=", 1)[0] for line in api_lines] == [
        "RELEASE_PUBLISHER_TOKEN", "RELEASE_OPERATOR_TOKEN", "GITHUB_WEBHOOK_SECRET", "RELEASE_STORAGE_ROOT"
    ]
    assert api_lines[-1] == "RELEASE_STORAGE_ROOT=/var/lib/eurith/releases"
    cleanup_lines = host.cleanup_env.read_text().splitlines()
    assert [line.split("=", 1)[0] for line in cleanup_lines] == ["DATABASE_URL", "RELEASE_STORAGE_ROOT"]
    assert cleanup_lines[-1] == "RELEASE_STORAGE_ROOT=/opt/eurith/releases"
    metadata = json.loads((host.state / "metadata.json").read_text())
    for path in (host.api_env, host.cleanup_env, host.deploy_env):
        assert metadata[str(path.resolve())] == {"mode": "640", "owner": "0", "group": "4321"}
    for path in (host.storage, host.storage / ".staging", host.storage / "android", host.storage / "android" / "sha256"):
        assert metadata[str(path.resolve())] == {"mode": "2770", "owner": "1234", "group": "4321"}


@pytest.mark.parametrize("filename", ["publisher-token", "operator-token", "webhook-secret"])
def test_malformed_token_is_rejected_without_installing_any_env(host: Host, filename: str) -> None:
    (host.secrets / filename).write_bytes(("A" * 64 + "\n").encode("ascii"))
    result = host.run()
    assert result.returncode != 0
    assert not host.api_env.exists(); assert not host.cleanup_env.exists()
    _assert_redacted(result)


def test_duplicate_tokens_and_missing_cleanup_source_are_rejected(host: Host) -> None:
    (host.secrets / "operator-token").write_bytes((TOKENS["publisher-token"] + "\n").encode("ascii"))
    duplicate = host.run()
    assert duplicate.returncode != 0
    _assert_redacted(duplicate)
    (host.secrets / "operator-token").write_bytes((TOKENS["operator-token"] + "\n").encode("ascii"))
    (host.secrets / "cleanup-database-url").unlink()
    missing = host.run()
    assert missing.returncode != 0
    _assert_redacted(missing)


def test_malformed_cleanup_database_source_is_rejected(host: Host) -> None:
    (host.secrets / "cleanup-database-url").write_bytes(b"not-a-postgresql-url\n")
    result = host.run()
    assert result.returncode != 0
    assert not host.cleanup_env.exists()
    _assert_redacted(result)


def test_partial_installation_fails_closed_without_overwrite(host: Host) -> None:
    assert host.run().returncode == 0
    host.cleanup_env.unlink()
    before = host.api_env.read_bytes()
    result = host.run()
    assert result.returncode != 0
    assert host.api_env.read_bytes() == before
    assert not host.cleanup_env.exists()


def test_existing_mode_or_key_drift_fails_closed(host: Host) -> None:
    assert host.run().returncode == 0
    metadata = json.loads((host.state / "metadata.json").read_text())
    metadata[str(host.api_env.resolve())]["mode"] = "644"
    (host.state / "metadata.json").write_text(json.dumps(metadata))
    mode_result = host.run()
    assert mode_result.returncode != 0
    metadata[str(host.api_env.resolve())]["mode"] = "640"
    (host.state / "metadata.json").write_text(json.dumps(metadata))
    host.api_env.write_text(host.api_env.read_text() + "EXTRA=value\n")
    key_result = host.run()
    assert key_result.returncode != 0
    _assert_redacted(key_result)


def test_existing_env_with_trailing_blank_line_is_not_an_exact_key_set(host: Host) -> None:
    assert host.run().returncode == 0
    host.cleanup_env.write_bytes(host.cleanup_env.read_bytes() + b"\n")
    result = host.run()
    assert result.returncode != 0
    _assert_redacted(result)


@pytest.mark.parametrize(
    ("path_kind", "mode", "owner", "group"),
    [
        ("source", "770", "0", "0"),
        ("source", "700", "1234", "0"),
        ("destination", "770", "0", "4321"),
        ("destination", "750", "1234", "4321"),
    ],
)
def test_unprotected_secret_or_destination_parent_is_rejected(
    host: Host, path_kind: str, mode: str, owner: str, group: str
) -> None:
    metadata = json.loads((host.state / "metadata.json").read_text())
    path = host.secrets if path_kind == "source" else host.etc
    metadata[str(path.resolve())] = {"mode": mode, "owner": owner, "group": group}
    (host.state / "metadata.json").write_text(json.dumps(metadata))
    result = host.run()
    assert result.returncode != 0
    assert not host.api_env.exists()
    _assert_redacted(result)


def test_secret_source_rejects_extra_file_subdirectory_and_symlink_entry(host: Host) -> None:
    for name, create in (
        ("extra", lambda path: path.write_text("x")),
        ("nested", lambda path: path.mkdir()),
        ("linked", lambda path: path.write_text("FAKE-SYMLINK:/etc/passwd")),
    ):
        extra = host.secrets / name
        create(extra)
        result = host.run()
        assert result.returncode != 0
        assert not host.api_env.exists()
        _assert_redacted(result)
        if extra.is_dir(): extra.rmdir()
        else: extra.unlink()


def test_secure_temp_collision_fails_without_touching_preplanted_entries(host: Host) -> None:
    planted_source = host.etc / ".api-release.env.source.preplanted"
    planted_install = host.etc / ".api-release.env.install.preplanted"
    planted_source.write_text("FAKE-SYMLINK:/etc/passwd")
    planted_install.write_bytes(b"owned")
    result = host.run(FAKE_MKTEMP_FAIL="1")
    assert result.returncode != 0
    assert planted_source.read_text() == "FAKE-SYMLINK:/etc/passwd"
    assert planted_install.read_bytes() == b"owned"
    assert not host.api_env.exists()


def test_preexisting_random_probe_collision_is_preserved(host: Host) -> None:
    assert host.run().returncode == 0
    token = "a" * 32
    fixture = host.storage / "android" / "sha256" / f".eurith-permission-probe-{token}"
    fixture.write_bytes(b"preexisting-owned-content")
    result = host.run(EURITH_TEST_PROBE_TOKEN=token)
    assert result.returncode != 0
    assert fixture.read_bytes() == b"preexisting-owned-content"


@pytest.mark.parametrize("stage", ["bind-final", "remount-final", "bind-probe", "remount-probe"])
def test_mount_failure_rolls_back_only_created_view_and_rerun_succeeds(host: Host, stage: str) -> None:
    failed = host.run(FAKE_MOUNT_FAIL_AT=stage)
    assert failed.returncode != 0
    assert not host.final_view.exists()
    assert not host.probe_view.exists()
    assert not host.probe_source.exists()
    rerun = host.run()
    assert rerun.returncode == 0, rerun.stderr


def test_failure_after_fixture_creation_removes_only_owned_fixture(host: Host) -> None:
    assert host.run().returncode == 0
    token = "b" * 32
    unrelated = host.storage / "android" / "sha256" / "unrelated.keep"
    unrelated.write_bytes(b"keep")
    result = host.run(EURITH_TEST_PROBE_TOKEN=token, FAKE_DOCKER_FAIL_ACTION="caddy-read")
    assert result.returncode != 0
    assert unrelated.read_bytes() == b"keep"
    assert not (unrelated.parent / f".eurith-permission-probe-{token}").exists()
    assert f".eurith-permission-probe-{token}" in (host.state / "calls.log").read_text()
    assert list(unrelated.parent.glob(".eurith-permission-probe-*")) == []


def test_ancestor_mount_cannot_satisfy_exact_mountpoint_gate(host: Host) -> None:
    assert host.run().returncode == 0
    mounts_path = host.state / "mounts.json"
    mounts = json.loads(mounts_path.read_text())
    exact = mounts.pop(str(host.final_view.resolve()))
    mounts[str(host.final_view.parent.resolve())] = exact
    mounts_path.write_text(json.dumps(mounts))
    result = host.run()
    assert result.returncode != 0
    assert "permissions=verified" not in result.stdout


def test_existing_symlink_destination_is_rejected(host: Host) -> None:
    host.api_env.write_text("placeholder\n")
    # Test-root symlink representation; production requires a real lstat symlink.
    host.api_env.write_text("FAKE-SYMLINK:/tmp/outside")
    result = host.run(EURITH_FAKE_DESTINATION_SYMLINK=str(host.api_env.resolve()))
    assert result.returncode != 0


@pytest.mark.parametrize("flag", ["FAKE_HOST_FOLLOWS_SYMLINK", "FAKE_CONTAINER_FOLLOWS_SYMLINK"])
def test_any_external_symlink_read_bypass_is_fatal(host: Host, flag: str) -> None:
    result = host.run(**{flag: "1"})
    assert result.returncode != 0
    assert "permissions=verified" not in result.stdout


@pytest.mark.parametrize("mutation", ["create", "replace", "chmod", "delete"])
def test_any_caddy_mutation_success_is_fatal(host: Host, mutation: str) -> None:
    result = host.run(FAKE_CADDY_MUTATION_SUCCEEDS=mutation)
    assert result.returncode != 0
    assert "permissions=verified" not in result.stdout


def test_missing_nosymfollow_or_direct_source_drift_is_rejected_on_rerun(host: Host) -> None:
    assert host.run().returncode == 0
    mounts_path = host.state / "mounts.json"
    mounts = json.loads(mounts_path.read_text())
    mounts[str(host.final_view.resolve())]["options"] = "ro"
    mounts_path.write_text(json.dumps(mounts))
    assert host.run().returncode != 0
    mounts[str(host.final_view.resolve())]["options"] = "ro,nosymfollow"
    mounts[str(host.final_view.resolve())]["source"] = str((host.storage / "android").resolve())
    mounts_path.write_text(json.dumps(mounts))
    assert host.run().returncode != 0


def test_runtime_commands_use_resolved_uid_gid_and_cleanup_through_api(host: Host) -> None:
    result = host.run()
    assert result.returncode == 0, result.stderr
    calls = (host.state / "calls.log").read_text()
    assert "--entrypoint id api -u" in calls
    assert "eurith-provision" in calls
    assert ".apk" not in "\n".join(line for line in calls.splitlines() if "EURITH_PROBE_ACTION=" in line)
    for action in ("api-stage", "container-gate", "caddy-read", "caddy-create", "caddy-replace", "caddy-chmod", "caddy-delete", "api-remove"):
        assert f"EURITH_PROBE_ACTION={action}" in calls
    assert calls.index("EURITH_PROBE_ACTION=api-stage") < calls.index("EURITH_PROBE_ACTION=caddy-read") < calls.index("EURITH_PROBE_ACTION=api-remove")
