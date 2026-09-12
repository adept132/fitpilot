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
BOOT_HELPER = ROOT / "deploy" / "systemd" / "eurith-release-views"
BOOT_SERVICE = ROOT / "deploy" / "systemd" / "eurith-release-views.service"
DOCKER_DROP_IN = ROOT / "deploy" / "systemd" / "docker-eurith-release-views.conf"
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
import json, os, pathlib, shutil, stat, subprocess, sys, tempfile

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

def boot_contract_is_valid():
    unit = pathlib.Path(os.environ["FAKE_BOOT_UNIT"])
    drop_in = pathlib.Path(os.environ["FAKE_DOCKER_DROP_IN"])
    if not unit.is_file() or not drop_in.is_file(): return False
    unit_text = unit.read_text()
    drop_in_text = drop_in.read_text()
    return all(value in unit_text for value in (
        "Before=docker.service",
        "Type=oneshot",
        "ExecStart=/usr/local/libexec/eurith-release-views",
        "RemainAfterExit=yes",
    )) and all(value in drop_in_text for value in (
        "Requires=eurith-release-views.service",
        "After=eurith-release-views.service",
    ))

def start_docker_through_dependency(emit_output=True):
    if not boot_contract_is_valid(): return 21
    result = subprocess.run(
        [os.environ["FAKE_BASH"], os.environ["EURITH_TEST_BOOT_HELPER"]],
        env=os.environ.copy(), text=True, capture_output=True, check=False,
    )
    if emit_output:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
    if result.returncode != 0: return result.returncode
    (state_dir / "docker-started").write_text("yes")
    return 0

def activate_boot_unit(emit_output=True):
    if (
        os.environ.get("FAKE_SYSTEMCTL_START_FAIL") == "1"
        or os.environ.get("FAKE_SYSTEMCTL_RESTART_FAIL") == "1"
    ): return 23
    result = subprocess.run(
        [os.environ["FAKE_BASH"], os.environ["EURITH_TEST_BOOT_HELPER"]],
        env=os.environ.copy(), text=True, capture_output=True, check=False,
    )
    if emit_output:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        (state_dir / "boot-unit-failed").write_text("yes")
        try: (state_dir / "boot-unit-active").unlink()
        except FileNotFoundError: pass
        return result.returncode
    (state_dir / "boot-unit-active").write_text("yes")
    try: (state_dir / "boot-unit-failed").unlink()
    except FileNotFoundError: pass
    return 0

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
    if "-s" in args:
        if target.replace("\\", "/").endswith("/etc/passwd"): target = "/etc/passwd"
        pathlib.Path(link).write_text("FAKE-SYMLINK:" + target)
        metadata_set(link, "777", "0", "0")
    else:
        source, destination = pathlib.Path(target), pathlib.Path(link)
        race = os.environ.get("FAKE_BOOT_ASSET_PUBLISH_RACE", "")
        if race and destination.name == "eurith-release-views" and not destination.exists():
            destination.write_bytes(source.read_bytes() if race == "exact" else b"competitor-won\n")
            record = load(metadata_path)[key(source)]
            metadata_set(destination, record["mode"], record["owner"], record["group"])
        if destination.exists(): sys.exit(1)
        os.link(source, destination)
        record = load(metadata_path)[key(source)]
        metadata_set(destination, record["mode"], record["owner"], record["group"])
        if (
            os.environ.get("FAKE_DOCKER_ACTIVATES_ON_BOOT_ASSET_PUBLISH") == "1"
            and destination.name == "eurith-release-views.conf"
        ):
            status = start_docker_through_dependency(emit_output=False)
            (state_dir / "publication-activation-status").write_text(str(status))
elif command == "readlink":
    if "-e" in args:
        target = pathlib.Path(args[-1])
        if not target.exists(): sys.exit(1)
        print(shell_path(target.resolve())); sys.exit(0)
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
    if args[:2] == ["build", "--quiet"]:
        if os.environ.get("FAKE_DOCKER_BUILD_FAIL") == "1": sys.exit(13)
        print(os.environ.get("FAKE_TARGET_IMAGE_ID", "sha256:" + "a" * 64)); sys.exit(0)
    if "--entrypoint id" in joined and joined.endswith(" -u") and " compose " not in (" " + joined + " "):
        count_path = state_dir / "api-uid-probe-count"
        count = int(count_path.read_text()) if count_path.exists() else 0
        count_path.write_text(str(count + 1))
        uid_key = "FAKE_COMPOSE_API_UID" if count else "FAKE_TARGET_API_UID"
        print(os.environ.get(uid_key, os.environ.get("FAKE_TARGET_API_UID", "1234"))); sys.exit(0)
    if args[:2] == ["image", "inspect"]:
        output = os.environ.get(
            "FAKE_COMPOSE_IMAGE_ID_OUTPUT",
            os.environ.get("FAKE_TARGET_IMAGE_ID", "sha256:" + "a" * 64),
        )
        sys.stdout.write(output + ("\n" if output else "")); sys.exit(0)
    is_overlay = args[:1] == ["compose"] and joined.count(" -f ") > 1
    if is_overlay and os.environ.get("FAKE_FAIL_OVERLAY_WITHOUT_API_ENV") == "1" and not pathlib.Path(os.environ["FAKE_API_ENV"]).exists():
        sys.exit(14)
    if is_overlay and os.environ.get("FAKE_DOCKER_AUTOCREATE_STORAGE") == "1":
        storage = pathlib.Path(os.environ["FAKE_STORAGE_ROOT"])
        if not storage.exists():
            storage.mkdir(parents=True); metadata_set(storage, "755", "0", "0")
    if is_overlay and joined.endswith(" build api"):
        sys.exit(0)
    if is_overlay and joined.endswith(" config --images api"):
        ref_output = os.environ.get("FAKE_COMPOSE_IMAGE_REF_OUTPUT", "eurith-api")
        sys.stdout.write(ref_output + ("\n" if ref_output else "")); sys.exit(0)
    if "--entrypoint id api -u" in joined:
        print(os.environ.get("FAKE_TARGET_API_UID", "1234") if is_overlay else "1234"); sys.exit(0)
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
elif command == "systemctl":
    if args == ["daemon-reload"]:
        if os.environ.get("FAKE_DOCKER_SOCKET_ACTIVATES_DURING_RELOAD") == "1":
            sys.exit(start_docker_through_dependency())
        sys.exit(0)
    if args == ["enable", "eurith-release-views.service"]:
        if os.environ.get("FAKE_SYSTEMCTL_ENABLE_FAIL") == "1": sys.exit(16)
        (state_dir / "boot-unit-enabled").write_text(os.environ.get("FAKE_SYSTEMCTL_ENABLE_STATE", "enabled"))
        sys.exit(0)
    if args == ["is-enabled", "--quiet", "eurith-release-views.service"]:
        if os.environ.get("FAKE_SYSTEMCTL_IS_ENABLED_FAIL") == "1": sys.exit(18)
        sys.exit(0 if (state_dir / "boot-unit-enabled").exists() else 1)
    if args == ["restart", "eurith-release-views.service"]:
        if (state_dir / "docker-started").exists():
            with (state_dir / "calls.log").open("a", encoding="utf-8") as log:
                log.write("systemctl try-restart docker.service\n")
            (state_dir / "docker-restart-propagated").write_text("yes")
        sys.exit(activate_boot_unit())
    if args == ["start", "eurith-release-views.service"]:
        if (state_dir / "boot-unit-active").exists(): sys.exit(0)
        sys.exit(activate_boot_unit())
    if args == ["is-enabled", "eurith-release-views.service"]:
        if os.environ.get("FAKE_SYSTEMCTL_IS_ENABLED_FAIL") == "1": sys.exit(18)
        state = os.environ.get("FAKE_SYSTEMCTL_ENABLE_STATE", "enabled")
        print(state)
        sys.exit(0 if state in ("enabled", "enabled-runtime") else 1)
    if args == ["is-active", "--quiet", "eurith-release-views.service"]:
        state = os.environ.get("FAKE_SYSTEMCTL_ACTIVE_STATE", "active")
        if state == "failed":
            (state_dir / "boot-unit-failed").write_text("yes")
            sys.exit(3)
        if state == "inactive": sys.exit(3)
        sys.exit(0 if (state_dir / "boot-unit-active").exists() else 3)
    if args == ["start", "docker.service"]:
        sys.exit(start_docker_through_dependency())
    sys.exit(2)
elif command == "systemd-analyze":
    if args and args[0] == "verify":
        if os.environ.get("FAKE_SYSTEMD_VERIFY_FAIL") == "1": sys.exit(17)
        sys.exit(0 if boot_contract_is_valid() else 22)
    sys.exit(2)
else:
    sys.exit(127)
'''


def _make_fake_commands(bin_dir: Path, state_dir: Path) -> None:
    driver = state_dir / "driver.py"
    driver.write_text(FAKE_DRIVER, encoding="utf-8")
    for command in ("docker", "findmnt", "mount", "umount", "getent", "groupadd", "install", "chown", "chmod", "stat", "ln", "readlink", "head", "mv", "mktemp", "find", "systemctl", "systemd-analyze"):
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
        self.rendered_boot_helper = tmp_path / "eurith-release-views"
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
        rendered = BOOT_HELPER.read_text(encoding="utf-8")
        for production, temporary in sorted(
            {
                "/opt/eurith/releases/android/sha256": _shell(self.storage / "android" / "sha256"),
                "/opt/eurith/release-caddy-view/android/sha256": _shell(self.final_view),
                "/opt/eurith/release-caddy-probe-source": _shell(self.probe_source),
                "/opt/eurith/release-caddy-view/.probe": _shell(self.probe_view),
            }.items(),
            key=lambda item: -len(item[0]),
        ):
            rendered = rendered.replace(production, temporary)
        self.rendered_boot_helper.write_text(rendered, encoding="utf-8", newline="\n")
        self.rendered_boot_helper.chmod(0o755)

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
    @property
    def installed_boot_helper(self) -> Path: return self.root / "usr" / "local" / "libexec" / "eurith-release-views"
    @property
    def installed_boot_service(self) -> Path: return self.root / "etc" / "systemd" / "system" / "eurith-release-views.service"
    @property
    def installed_docker_drop_in(self) -> Path: return self.root / "etc" / "systemd" / "system" / "docker.service.d" / "eurith-release-views.conf"

    def environment(self, **extra_env: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "PATH": _shell(self.bin) + ":/usr/bin:/bin",
            "FAKE_STATE_DIR": str(self.state),
            "FAKE_FINAL_DIR": str(self.storage / "android" / "sha256"),
            "FAKE_STAGING_DIR": str(self.storage / ".staging"),
            "FAKE_STORAGE_ROOT": str(self.storage),
            "FAKE_API_ENV": str(self.api_env),
            "FAKE_FINAL_VIEW": str(self.final_view),
            "FAKE_PROBE_VIEW": str(self.probe_view),
            "FAKE_PROBE_SOURCE": str(self.probe_source),
            "FAKE_BOOT_UNIT": str(self.installed_boot_service),
            "FAKE_DOCKER_DROP_IN": str(self.installed_docker_drop_in),
            "FAKE_BASH": BASH,
            "EURITH_FAKE_SYMLINKS": "1",
            "EURITH_TEST_BOOT_HELPER": _shell(self.rendered_boot_helper),
            "CADDY_IMAGE_REF": "caddy:2.11.4@sha256:" + "c" * 64,
        })
        env.update(extra_env)
        return env

    def run(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = self.environment(**extra_env)
        args = [
            BASH, _shell(SCRIPT), "--root", _shell(self.root),
            "--secret-source-dir", _shell(self.secrets),
            "--api-release-env", _shell(self.api_env),
            "--cleanup-env", _shell(self.cleanup_env),
            "--deploy-env", _shell(self.deploy_env),
            "--base-compose", _shell(self.base_compose),
            "--release-overlay", _shell(ROOT / "deploy" / "compose.release.yml"),
            "--site-address", "https://api.eurith.app",
            "--deploy-asset-root", _shell(ROOT),
            "--target-sha", "a" * 40,
        ]
        return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, check=False)

    def start_docker(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, _shell(self.bin / "systemctl"), "start", "docker.service"],
            cwd=ROOT,
            env=self.environment(**extra_env),
            text=True,
            capture_output=True,
            check=False,
        )


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
    assert BOOT_HELPER.is_file()
    assert BOOT_SERVICE.is_file()
    assert DOCKER_DROP_IN.is_file()


def test_boot_assets_are_installed_exactly_and_ordered_before_permission_probes(host: Host) -> None:
    result = host.run()

    assert result.returncode == 0, result.stderr
    installed = (
        (host.installed_boot_helper, BOOT_HELPER, "755"),
        (host.installed_boot_service, BOOT_SERVICE, "644"),
        (host.installed_docker_drop_in, DOCKER_DROP_IN, "644"),
    )
    metadata = json.loads((host.state / "metadata.json").read_text())
    for destination, source, mode in installed:
        assert destination.read_bytes() == source.read_bytes()
        assert metadata[str(destination.resolve())] == {
            "mode": mode,
            "owner": "0",
            "group": "0",
        }

    service = host.installed_boot_service.read_text(encoding="utf-8")
    for line in (
        "Type=oneshot",
        "RemainAfterExit=yes",
        "Before=docker.service",
        "RequiresMountsFor=/opt/eurith/releases /opt/eurith/release-caddy-probe-source",
        "WantedBy=multi-user.target",
        "ExecStart=/usr/local/libexec/eurith-release-views",
    ):
        assert line in service
    drop_in = host.installed_docker_drop_in.read_text(encoding="utf-8")
    assert "Requires=eurith-release-views.service" in drop_in
    assert "After=eurith-release-views.service" in drop_in

    calls = (host.state / "calls.log").read_text().splitlines()
    daemon_reload = calls.index("systemctl daemon-reload")
    graph_verify = next(index for index, call in enumerate(calls) if call.startswith("systemd-analyze verify "))
    enable = calls.index("systemctl enable eurith-release-views.service")
    start = calls.index("systemctl start eurith-release-views.service")
    enabled_gate = calls.index("systemctl is-enabled eurith-release-views.service")
    active_gate = calls.index("systemctl is-active --quiet eurith-release-views.service")
    permission_probe = next(index for index, call in enumerate(calls) if "EURITH_PROBE_ACTION=api-stage" in call)
    assert daemon_reload < graph_verify < enable < start < enabled_gate < active_gate < permission_probe
    assert (host.state / "boot-unit-active").read_text() == "yes"
    assert result.stdout.index("boot_mounts=verified") < result.stdout.index("permissions=verified")


def test_boot_asset_installation_and_enablement_are_idempotent(host: Host) -> None:
    first = host.run()
    assert first.returncode == 0, first.stderr
    destinations = (
        host.installed_boot_helper,
        host.installed_boot_service,
        host.installed_docker_drop_in,
    )
    expected_bytes = {path: path.read_bytes() for path in destinations}
    (host.state / "calls.log").write_text("", encoding="utf-8")

    repeated = host.run()

    assert repeated.returncode == 0, repeated.stderr
    assert repeated.stdout.count("boot_mounts=verified") == 1
    assert {path: path.read_bytes() for path in destinations} == expected_bytes
    calls = (host.state / "calls.log").read_text().splitlines()
    assert calls.count("systemctl daemon-reload") == 1
    assert calls.count("systemctl enable eurith-release-views.service") == 1
    assert calls.count("systemctl start eurith-release-views.service") == 1
    assert calls.count("systemctl is-enabled eurith-release-views.service") == 1
    assert calls.count("systemctl is-active --quiet eurith-release-views.service") == 1
    assert (host.state / "boot-unit-active").read_text() == "yes"


def test_repeat_provisioning_with_active_docker_does_not_restart_docker(host: Host) -> None:
    first = host.run()
    assert first.returncode == 0, first.stderr
    docker = host.start_docker()
    assert docker.returncode == 0, docker.stderr
    (host.state / "calls.log").write_text("", encoding="utf-8")

    repeated = host.run()

    assert repeated.returncode == 0, repeated.stderr
    assert (host.state / "docker-started").read_text() == "yes"
    assert not (host.state / "docker-restart-propagated").exists()
    calls = (host.state / "calls.log").read_text().splitlines()
    assert "systemctl try-restart docker.service" not in calls
    assert "systemctl restart docker.service" not in calls
    assert "systemctl stop docker.service" not in calls


def test_repeat_provisioning_with_active_docker_revalidates_live_mounts(host: Host) -> None:
    first = host.run()
    assert first.returncode == 0, first.stderr
    docker = host.start_docker()
    assert docker.returncode == 0, docker.stderr
    mounts_path = host.state / "mounts.json"
    mounts = json.loads(mounts_path.read_text())
    mounts[str(host.final_view.resolve())]["options"] = "rw,nosymfollow"
    mounts_path.write_text(json.dumps(mounts), encoding="utf-8")
    (host.state / "calls.log").write_text("", encoding="utf-8")

    repeated = host.run()

    assert repeated.returncode != 0
    assert "error=mount_readonly_missing" in repeated.stderr
    assert "permissions=verified" not in repeated.stdout
    assert (host.state / "docker-started").read_text() == "yes"
    assert not (host.state / "docker-restart-propagated").exists()
    calls = (host.state / "calls.log").read_text().splitlines()
    assert "systemctl try-restart docker.service" not in calls
    assert "systemctl restart docker.service" not in calls
    assert "systemctl stop docker.service" not in calls


@pytest.mark.parametrize(
    ("extra_env", "expected_error"),
    [
        ({"FAKE_SYSTEMCTL_ENABLE_STATE": "enabled-runtime"}, "boot_unit_not_persistently_enabled"),
        ({"FAKE_SYSTEMCTL_ENABLE_STATE": "disabled"}, "boot_unit_not_persistently_enabled"),
        ({"FAKE_SYSTEMCTL_ACTIVE_STATE": "inactive"}, "boot_unit_not_active"),
        ({"FAKE_SYSTEMCTL_ACTIVE_STATE": "failed"}, "boot_unit_not_active"),
    ],
)
def test_boot_unit_requires_persistent_enabled_and_active_state(
    host: Host, extra_env: dict[str, str], expected_error: str
) -> None:
    result = host.run(**extra_env)

    assert result.returncode != 0
    assert f"error={expected_error}" in result.stderr
    assert "permissions=verified" not in result.stdout


def test_boot_unit_start_failure_blocks_provisioning(host: Host) -> None:
    result = host.run(FAKE_SYSTEMCTL_START_FAIL="1")

    assert result.returncode != 0
    assert "error=boot_unit_start_failed" in result.stderr
    assert "boot_mounts=verified" not in result.stdout
    assert "permissions=verified" not in result.stdout


@pytest.mark.parametrize(("race", "succeeds"), [("exact", True), ("drift", False)])
def test_boot_asset_publication_never_overwrites_a_concurrent_destination(
    host: Host, race: str, succeeds: bool
) -> None:
    result = host.run(FAKE_BOOT_ASSET_PUBLISH_RACE=race)

    assert (result.returncode == 0) is succeeds
    if succeeds:
        assert host.installed_boot_helper.read_bytes() == BOOT_HELPER.read_bytes()
        assert "permissions=verified" in result.stdout
    else:
        assert host.installed_boot_helper.read_bytes() == b"competitor-won\n"
        assert "error=boot_asset_bytes_mismatch" in result.stderr
        assert "permissions=verified" not in result.stdout


def test_first_use_activation_waits_until_release_layout_exists(host: Host) -> None:
    result = host.run(FAKE_DOCKER_SOCKET_ACTIVATES_DURING_RELOAD="1")

    assert result.returncode == 0, result.stderr
    assert (host.state / "docker-started").is_file()
    assert "boot_mounts=verified" in result.stdout
    assert "permissions=verified" in result.stdout


def test_external_activation_at_asset_publication_sees_complete_layout(host: Host) -> None:
    result = host.run(FAKE_DOCKER_ACTIVATES_ON_BOOT_ASSET_PUBLISH="1")

    assert result.returncode == 0, result.stderr
    assert (host.state / "publication-activation-status").read_text() == "0"
    assert (host.state / "docker-started").is_file()
    assert host.probe_source.is_dir()
    assert (host.probe_source / "regular").is_file()
    mounts = json.loads((host.state / "mounts.json").read_text())
    assert set(mounts) == {str(host.final_view.resolve()), str(host.probe_view.resolve())}
    assert "permissions=verified" in result.stdout


def test_docker_dependency_blocks_start_when_helper_fails(host: Host) -> None:
    assert host.run().returncode == 0
    (host.state / "mounts.json").write_text("{}", encoding="utf-8")

    result = host.start_docker(FAKE_MOUNT_FAIL_AT="bind-final")

    assert result.returncode != 0
    assert "error=mount_bind_failed" in result.stderr
    assert not (host.state / "docker-started").exists()


def test_docker_dependency_restores_views_before_start(host: Host) -> None:
    assert host.run().returncode == 0
    (host.state / "mounts.json").write_text("{}", encoding="utf-8")

    result = host.start_docker()

    assert result.returncode == 0, result.stderr
    assert (host.state / "docker-started").is_file()
    mounts = json.loads((host.state / "mounts.json").read_text())
    assert set(mounts) == {
        str(host.final_view.resolve()),
        str(host.probe_view.resolve()),
    }


@pytest.mark.parametrize(
    ("drift", "expected_error"),
    [
        ("bytes", "boot_asset_bytes_mismatch"),
        ("mode", "protected_metadata_mismatch"),
        ("owner", "protected_metadata_mismatch"),
        ("type", "boot_asset_invalid"),
        ("symlink", "boot_asset_invalid"),
    ],
)
def test_preexisting_boot_asset_drift_fails_closed_without_overwrite(
    host: Host, drift: str, expected_error: str
) -> None:
    assert host.run().returncode == 0
    destination = host.installed_boot_helper
    extra_env: dict[str, str] = {}
    if drift == "bytes":
        destination.write_bytes(b"unexpected\n")
    elif drift in ("mode", "owner"):
        metadata_path = host.state / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata[str(destination.resolve())][drift] = "700" if drift == "mode" else "1234"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif drift == "type":
        destination.unlink()
        destination.mkdir()
    else:
        destination.write_text("FAKE-SYMLINK:/outside", encoding="utf-8")
        extra_env["EURITH_FAKE_BOOT_ASSET_SYMLINK"] = _shell(destination)
    before = destination.read_bytes() if destination.is_file() else None

    result = host.run(**extra_env)

    assert result.returncode != 0
    assert f"error={expected_error}" in result.stderr
    assert "boot_mounts=verified" not in result.stdout
    assert "permissions=verified" not in result.stdout
    if before is not None:
        assert destination.read_bytes() == before


@pytest.mark.parametrize(
    ("extra_env", "expected_error"),
    [
        ({"FAKE_SYSTEMD_VERIFY_FAIL": "1"}, "boot_systemd_verify_failed"),
        ({"FAKE_SYSTEMCTL_ENABLE_FAIL": "1"}, "boot_unit_enable_failed"),
        ({"FAKE_SYSTEMCTL_IS_ENABLED_FAIL": "1"}, "boot_unit_not_persistently_enabled"),
        ({"FAKE_MOUNT_FAIL_AT": "bind-final"}, "mount_bind_failed"),
    ],
)
def test_boot_setup_failure_prevents_permission_verification(
    host: Host, extra_env: dict[str, str], expected_error: str
) -> None:
    result = host.run(**extra_env)

    assert result.returncode != 0
    assert f"error={expected_error}" in result.stderr
    assert "permissions=verified" not in result.stdout
    calls = (host.state / "calls.log").read_text()
    assert "EURITH_PROBE_ACTION=api-stage" not in calls


@pytest.mark.parametrize(
    "extra_env",
    [
        {"FAKE_SYSTEMCTL_IS_ENABLED_FAIL": "1"},
        {"FAKE_DOCKER_FAIL_ACTION": "caddy-read"},
    ],
)
def test_first_use_failure_after_helper_preserves_mounted_layout_for_retry(
    host: Host, extra_env: dict[str, str]
) -> None:
    failed = host.run(**extra_env)

    assert failed.returncode != 0
    mounts = json.loads((host.state / "mounts.json").read_text())
    assert set(mounts) == {
        str(host.final_view.resolve()),
        str(host.probe_view.resolve()),
    }
    assert host.probe_source.is_dir()
    assert (host.probe_source / "regular").is_file()

    retried = host.run()

    assert retried.returncode == 0, retried.stderr
    assert "boot_mounts=verified" in retried.stdout
    assert "permissions=verified" in retried.stdout


@pytest.mark.parametrize(
    "failure_env",
    [
        {"FAKE_SYSTEMD_VERIFY_FAIL": "1"},
        {"FAKE_SYSTEMCTL_ENABLE_FAIL": "1"},
    ],
)
def test_activation_before_verify_or_enable_failure_retains_layout_for_retry(
    host: Host, failure_env: dict[str, str]
) -> None:
    failed = host.run(
        FAKE_DOCKER_SOCKET_ACTIVATES_DURING_RELOAD="1",
        **failure_env,
    )

    assert failed.returncode != 0
    assert (host.state / "docker-started").is_file()
    mounts = json.loads((host.state / "mounts.json").read_text())
    assert set(mounts) == {str(host.final_view.resolve()), str(host.probe_view.resolve())}
    assert host.probe_source.is_dir()
    assert (host.probe_source / "regular").is_file()

    retried = host.run()

    assert retried.returncode == 0, retried.stderr
    assert "boot_mounts=verified" in retried.stdout
    assert "permissions=verified" in retried.stdout


def test_target_uid_is_resolved_before_storage_and_any_release_overlay() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    target_build = script.index('TARGET_API_IMAGE="$(docker build --quiet --file ')
    target_probe = script.index('API_UID="$(docker run --rm --entrypoint id ')
    storage_create = script.index('install -d -m 2770 -o "$API_UID"')
    storage_verify = script.index('for directory in "$STORAGE_ROOT"')
    overlay_compose = script.index('compose=(docker compose --env-file "$DEPLOY_ENV" -f "$BASE_COMPOSE" -f "$RELEASE_OVERLAY")')
    assert target_build < target_probe < storage_create < storage_verify < overlay_compose
    assert '[[ "$TARGET_API_IMAGE" =~ ^(sha256:)?[0-9a-f]{64}$ ]]' in script


def test_first_use_and_repeat_are_idempotent_and_redacted(host: Host) -> None:
    first = host.run()
    assert first.returncode == 0, first.stderr
    assert first.stdout.splitlines() == [
        "boot_mounts=verified",
        "group=created", "storage=created", "release_view=created",
        "api_env=created", "cleanup_env=created", "deploy_env=updated",
        "permissions=verified",
    ]
    _assert_redacted(first)
    second = host.run()
    assert second.returncode == 0, second.stderr
    assert second.stdout.splitlines() == [
        "boot_mounts=verified",
        "group=existing", "storage=verified", "release_view=verified",
        "api_env=existing", "cleanup_env=existing", "deploy_env=updated",
        "permissions=verified",
    ]
    _assert_redacted(second)
    assert host.deploy_env.read_text() == "RELEASE_SHARED_GID=4321\nEURITH_SITE_ADDRESS=https://api.eurith.app\n"
    assert not list((host.storage / ".staging").iterdir())
    assert not list((host.storage / "android" / "sha256").iterdir())


def test_first_use_survives_real_compose_short_bind_autocreate_side_effect(host: Host) -> None:
    result = host.run(FAKE_DOCKER_AUTOCREATE_STORAGE="1")
    assert result.returncode == 0, result.stderr
    metadata = json.loads((host.state / "metadata.json").read_text())
    assert metadata[str(host.storage.resolve())] == {
        "mode": "2770", "owner": "1234", "group": "4321"
    }


def test_first_use_builds_target_and_resolves_uid_without_release_env_or_overlay(host: Host) -> None:
    result = host.run(
        FAKE_FAIL_OVERLAY_WITHOUT_API_ENV="1",
        FAKE_TARGET_API_UID="2345",
    )

    assert result.returncode == 0, result.stderr
    calls = (host.state / "calls.log").read_text().splitlines()
    docker_calls = [line for line in calls if line.startswith("docker ")]
    assert docker_calls[0].startswith("docker build --quiet --file ")
    assert ROOT.resolve().as_posix() in docker_calls[0]
    assert "docker run --rm --entrypoint id sha256:" in docker_calls[1]
    first_overlay = next(index for index, line in enumerate(docker_calls) if line.count(" -f ") > 1)
    assert first_overlay > 1
    metadata = json.loads((host.state / "metadata.json").read_text())
    assert metadata[str(host.storage.resolve())] == {
        "mode": "2770", "owner": "2345", "group": "4321"
    }


@pytest.mark.parametrize(
    ("extra_env", "expected_error"),
    [
        ({"FAKE_DOCKER_BUILD_FAIL": "1"}, "target_api_image_build_failed"),
        ({"FAKE_TARGET_IMAGE_ID": "not-an-image-id"}, "target_api_image_id_invalid"),
    ],
)
def test_target_image_resolution_fails_closed_before_storage_or_env(
    host: Host, extra_env: dict[str, str], expected_error: str
) -> None:
    result = host.run(**extra_env)

    assert result.returncode != 0
    assert f"error={expected_error}" in result.stderr
    assert not host.storage.exists()
    assert not host.api_env.exists()
    assert not host.cleanup_env.exists()


def test_target_image_uid_is_authoritative_for_new_storage(host: Host) -> None:
    result = host.run(FAKE_TARGET_API_UID="2345")
    assert result.returncode == 0, result.stderr
    metadata = json.loads((host.state / "metadata.json").read_text())
    assert metadata[str(host.storage.resolve())]["owner"] == "2345"
    assert "permissions=verified" in result.stdout


@pytest.mark.parametrize(
    ("extra_env", "expected_error"),
    [
        ({"FAKE_COMPOSE_IMAGE_ID_OUTPUT": "sha256:" + "b" * 64}, "compose_api_image_mismatch"),
        ({"FAKE_COMPOSE_IMAGE_REF_OUTPUT": ""}, "compose_api_image_ref_invalid"),
        (
            {"FAKE_COMPOSE_IMAGE_REF_OUTPUT": "eurith-api\nother-api"},
            "compose_api_image_ref_invalid",
        ),
        ({"FAKE_COMPOSE_IMAGE_ID_OUTPUT": ""}, "compose_api_image_id_invalid"),
        (
            {"FAKE_COMPOSE_IMAGE_ID_OUTPUT": "sha256:" + "a" * 64 + "\nsha256:" + "b" * 64},
            "compose_api_image_id_invalid",
        ),
        ({"FAKE_COMPOSE_API_UID": "2345"}, "compose_api_uid_mismatch"),
    ],
)
def test_compose_image_identity_fails_closed_before_permission_probes(
    host: Host, extra_env: dict[str, str], expected_error: str
) -> None:
    result = host.run(**extra_env)

    assert result.returncode != 0
    assert f"error={expected_error}" in result.stderr
    assert "permissions=verified" not in result.stdout
    calls = (host.state / "calls.log").read_text()
    assert "EURITH_PROBE_ACTION=" not in calls


def test_compose_image_id_without_prefix_is_normalized(host: Host) -> None:
    result = host.run(
        FAKE_TARGET_IMAGE_ID="a" * 64,
        FAKE_COMPOSE_IMAGE_REF_OUTPUT="registry.example:5000/team/api:production",
        FAKE_COMPOSE_IMAGE_ID_OUTPUT="a" * 64,
    )

    assert result.returncode == 0, result.stderr
    assert "permissions=verified" in result.stdout


def test_preexisting_unsafe_storage_fails_before_overlay_compose(host: Host) -> None:
    host.storage.mkdir(parents=True)
    metadata_path = host.state / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata[str(host.storage.resolve())] = {"mode": "755", "owner": "0", "group": "0"}
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = host.run()

    assert result.returncode != 0
    assert "error=protected_metadata_mismatch" in result.stderr
    docker_calls = [
        line for line in (host.state / "calls.log").read_text().splitlines()
        if line.startswith("docker ")
    ]
    assert len(docker_calls) == 2
    assert docker_calls[0].startswith("docker build --quiet --file ")
    assert docker_calls[1].startswith("docker run --rm --entrypoint id sha256:")


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
def test_mount_failure_retains_complete_view_and_rerun_succeeds(host: Host, stage: str) -> None:
    failed = host.run(FAKE_MOUNT_FAIL_AT=stage)
    assert failed.returncode != 0
    assert host.final_view.is_dir()
    assert host.probe_view.is_dir()
    assert host.probe_source.is_dir()
    assert (host.probe_source / "regular").is_file()
    mounts_path = host.state / "mounts.json"
    assert not mounts_path.exists() or json.loads(mounts_path.read_text()) == {}
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
    assert "docker build --quiet --file " in calls
    assert "docker run --rm --entrypoint id sha256:" in calls
    assert " build api" in calls
    assert "eurith-provision" in calls
    assert ".apk" not in "\n".join(line for line in calls.splitlines() if "EURITH_PROBE_ACTION=" in line)
    for action in ("api-stage", "container-gate", "caddy-read", "caddy-create", "caddy-replace", "caddy-chmod", "caddy-delete", "api-remove"):
        assert f"EURITH_PROBE_ACTION={action}" in calls
    assert calls.index("EURITH_PROBE_ACTION=api-stage") < calls.index("EURITH_PROBE_ACTION=caddy-read") < calls.index("EURITH_PROBE_ACTION=api-remove")
