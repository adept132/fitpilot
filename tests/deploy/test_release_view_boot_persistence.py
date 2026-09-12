from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy" / "systemd" / "eurith-release-views"
BASH = shutil.which("bash") or "D:/Git/usr/bin/bash.exe"

PRODUCTION_PATHS = {
    "final_source": "/opt/eurith/releases/android/sha256",
    "final_target": "/opt/eurith/release-caddy-view/android/sha256",
    "probe_source": "/opt/eurith/release-caddy-probe-source",
    "probe_target": "/opt/eurith/release-caddy-view/.probe",
}


def _shell(path: Path) -> str:
    value = path.resolve().as_posix()
    if len(value) > 2 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _call_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


FAKE_DRIVER = r'''from __future__ import annotations
import json, os, pathlib, sys

command, *args = sys.argv[1:]
state_dir = pathlib.Path(os.environ["FAKE_STATE_DIR"])
mounts_path = state_dir / "mounts.json"
calls_path = state_dir / "calls.log"

def load_mounts():
    return json.loads(mounts_path.read_text()) if mounts_path.exists() else {}

def save_mounts(mounts):
    mounts_path.write_text(json.dumps(mounts, sort_keys=True))

def key(value):
    return str(pathlib.Path(value).resolve())

def shell_path(value):
    value = str(value).replace("\\", "/")
    if len(value) > 2 and value[1] == ":":
        return "/" + value[0].lower() + value[2:]
    return value

with calls_path.open("a", encoding="utf-8") as log:
    log.write(command + " " + " ".join(args) + "\n")

if command == "mount":
    mounts = load_mounts()
    target = key(args[-1])
    target_kind = "final" if target == key(os.environ["FAKE_FINAL_TARGET"]) else "probe"
    operation = "bind" if "--bind" in args else "remount"
    if os.environ.get("FAKE_MOUNT_FAIL_AT") == f"{operation}-{target_kind}":
        sys.exit(19)
    if operation == "bind":
        mounts[target] = {
            "source": key(args[-2]),
            "target": target,
            "options": "rw,nosuid,nodev",
        }
    else:
        if target not in mounts:
            sys.exit(20)
        mounts[target]["options"] = "ro,nosymfollow,nosuid,nodev"
    save_mounts(mounts)
elif command == "umount":
    mounts = load_mounts()
    mounts.pop(key(args[-1]), None)
    save_mounts(mounts)
elif command == "findmnt":
    mounts = load_mounts()
    record = mounts.get(key(args[-1]))
    if record is None:
        sys.exit(1)
    if "SOURCE" in args:
        if os.environ.get("FAKE_REALISTIC_BIND_SOURCE") == "1":
            print("/dev/fake[" + shell_path(record["source"]) + "]")
        else:
            print(shell_path(record["source"]))
    elif "TARGET" in args:
        print(shell_path(record["target"]))
    elif "VFS-OPTIONS" in args:
        print(record["options"])
    else:
        sys.exit(2)
elif command == "readlink":
    target = key(args[-1])
    symlink_component = os.environ.get("FAKE_SYMLINK_COMPONENT", "")
    if symlink_component:
        symlink_component = key(symlink_component)
        if target == symlink_component or target.startswith(symlink_component + os.sep):
            target = symlink_component + ".resolved" + target[len(symlink_component):]
    if not pathlib.Path(args[-1]).exists():
        sys.exit(1)
    print(shell_path(target))
elif command == "stat":
    mounts = load_mounts()
    target = key(args[-1])
    record = mounts.get(target)
    print(record["source"] if record is not None else target)
else:
    sys.exit(127)
'''


class HelperHost:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "fixed-root"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        self.rendered = tmp_path / "eurith-release-views"
        self.final_source = self.root / "opt" / "eurith" / "releases" / "android" / "sha256"
        self.final_target = self.root / "opt" / "eurith" / "release-caddy-view" / "android" / "sha256"
        self.probe_source = self.root / "opt" / "eurith" / "release-caddy-probe-source"
        self.probe_target = self.root / "opt" / "eurith" / "release-caddy-view" / ".probe"
        for path in (
            self.final_source,
            self.final_target,
            self.probe_source,
            self.probe_target,
            self.state,
            self.bin,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.mounts_path.write_text("{}", encoding="utf-8")
        self.calls_path.write_text("", encoding="utf-8")
        self._render_helper()
        self._make_fake_commands()

    @property
    def mounts_path(self) -> Path:
        return self.state / "mounts.json"

    @property
    def calls_path(self) -> Path:
        return self.state / "calls.log"

    def _render_helper(self) -> None:
        source = HELPER.read_text(encoding="utf-8")
        replacements = {
            PRODUCTION_PATHS["final_source"]: _shell(self.final_source),
            PRODUCTION_PATHS["final_target"]: _shell(self.final_target),
            PRODUCTION_PATHS["probe_source"]: _shell(self.probe_source),
            PRODUCTION_PATHS["probe_target"]: _shell(self.probe_target),
        }
        for production, rendered in sorted(replacements.items(), key=lambda item: -len(item[0])):
            source = source.replace(production, rendered)
        self.rendered.write_text(source, encoding="utf-8", newline="\n")
        self.rendered.chmod(0o755)

    def _make_fake_commands(self) -> None:
        driver = self.state / "driver.py"
        driver.write_text(FAKE_DRIVER, encoding="utf-8")
        for command in ("mount", "umount", "findmnt", "readlink", "stat"):
            wrapper = self.bin / command
            wrapper.write_text(
                f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{driver.as_posix()}" {command} "$@"\n',
                encoding="utf-8",
                newline="\n",
            )
            wrapper.chmod(0o755)

    def run(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": _shell(self.bin) + ":/usr/bin:/bin",
                "FAKE_STATE_DIR": str(self.state),
                "FAKE_FINAL_TARGET": str(self.final_target),
            }
        )
        env.update(extra_env)
        return subprocess.run(
            [BASH, _shell(self.rendered)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def mounts(self) -> dict[str, dict[str, str]]:
        return json.loads(self.mounts_path.read_text())

    def calls(self) -> list[str]:
        return self.calls_path.read_text().splitlines()

    def clear_calls(self) -> None:
        self.calls_path.write_text("", encoding="utf-8")

    def set_mount(
        self,
        source: Path,
        target: Path,
        options: str = "ro,nosymfollow,nosuid,nodev",
    ) -> None:
        mounts = self.mounts()
        target_key = str(target.resolve())
        mounts[target_key] = {
            "source": str(source.resolve()),
            "target": target_key,
            "options": options,
        }
        self.mounts_path.write_text(json.dumps(mounts, sort_keys=True), encoding="utf-8")


@pytest.fixture
def helper_host(tmp_path: Path) -> HelperHost:
    return HelperHost(tmp_path)


def _expected_mounts(host: HelperHost) -> dict[str, dict[str, str]]:
    return {
        str(host.final_target.resolve()): {
            "source": str(host.final_source.resolve()),
            "target": str(host.final_target.resolve()),
            "options": "ro,nosymfollow,nosuid,nodev",
        },
        str(host.probe_target.resolve()): {
            "source": str(host.probe_source.resolve()),
            "target": str(host.probe_target.resolve()),
            "options": "ro,nosymfollow,nosuid,nodev",
        },
    }


def _assert_failure(
    result: subprocess.CompletedProcess[str],
    error_code: str,
    remaining_mounts: dict[str, dict[str, str]],
    host: HelperHost,
) -> None:
    assert result.returncode != 0
    assert f"error={error_code}" in result.stderr
    assert "boot_mounts=verified" not in result.stdout
    assert host.mounts() == remaining_mounts


def test_first_start_creates_and_verifies_both_exact_mounts(helper_host: HelperHost) -> None:
    result = helper_host.run()

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["boot_mounts=verified"]
    assert helper_host.mounts() == _expected_mounts(helper_host)
    mount_calls = [call for call in helper_host.calls() if call.startswith("mount ")]
    assert mount_calls == [
        f"mount --bind {_call_path(helper_host.final_source)} {_call_path(helper_host.final_target)}",
        f"mount -o remount,bind,ro,nosymfollow {_call_path(helper_host.final_target)}",
        f"mount --bind {_call_path(helper_host.probe_source)} {_call_path(helper_host.probe_target)}",
        f"mount -o remount,bind,ro,nosymfollow {_call_path(helper_host.probe_target)}",
    ]


def test_repeat_is_verification_only_and_does_not_mutate_mounts(helper_host: HelperHost) -> None:
    assert helper_host.run().returncode == 0
    expected = helper_host.mounts()
    helper_host.clear_calls()

    repeated = helper_host.run()

    assert repeated.returncode == 0, repeated.stderr
    assert repeated.stdout.splitlines() == ["boot_mounts=verified"]
    assert helper_host.mounts() == expected
    assert not any(call.startswith(("mount ", "umount ")) for call in helper_host.calls())


def test_reboot_like_missing_mounts_are_restored(helper_host: HelperHost) -> None:
    assert helper_host.run().returncode == 0
    helper_host.mounts_path.write_text("{}", encoding="utf-8")
    helper_host.clear_calls()

    restored = helper_host.run()

    assert restored.returncode == 0, restored.stderr
    assert restored.stdout.splitlines() == ["boot_mounts=verified"]
    assert helper_host.mounts() == _expected_mounts(helper_host)
    assert len([call for call in helper_host.calls() if call.startswith("mount ")]) == 4


def test_wrong_existing_source_fails_without_repair_or_unmount(helper_host: HelperHost) -> None:
    wrong_source = helper_host.final_source.parent
    helper_host.set_mount(wrong_source, helper_host.final_target)
    existing = helper_host.mounts()

    result = helper_host.run()

    _assert_failure(result, "mount_source_mismatch", existing, helper_host)
    assert not any(call.startswith(("mount ", "umount ")) for call in helper_host.calls())


def test_realistic_bind_source_format_is_verified_by_directory_identity(
    helper_host: HelperHost,
) -> None:
    result = helper_host.run(FAKE_REALISTIC_BIND_SOURCE="1")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["boot_mounts=verified"]
    assert helper_host.mounts() == _expected_mounts(helper_host)


@pytest.mark.parametrize(
    ("options", "error_code"),
    [
        ("rw,nosymfollow,nosuid", "mount_readonly_missing"),
        ("ro,nosuid,nodev", "mount_nosymfollow_missing"),
    ],
)
def test_existing_mount_missing_required_option_fails_without_repair(
    helper_host: HelperHost, options: str, error_code: str
) -> None:
    helper_host.set_mount(helper_host.final_source, helper_host.final_target, options)
    existing = helper_host.mounts()

    result = helper_host.run()

    _assert_failure(result, error_code, existing, helper_host)
    assert not any(call.startswith(("mount ", "umount ")) for call in helper_host.calls())


SYMLINK_CASES = [
    (path_name, component_index)
    for path_name, relative_parts in (
        ("final_source", ("opt", "eurith", "releases", "android", "sha256")),
        ("final_target", ("opt", "eurith", "release-caddy-view", "android", "sha256")),
        ("probe_source", ("opt", "eurith", "release-caddy-probe-source")),
        ("probe_target", ("opt", "eurith", "release-caddy-view", ".probe")),
    )
    for component_index in range(len(relative_parts))
]


@pytest.mark.parametrize(("path_name", "component_index"), SYMLINK_CASES)
def test_every_source_and_destination_component_symlink_fails_closed(
    helper_host: HelperHost, path_name: str, component_index: int
) -> None:
    path = getattr(helper_host, path_name)
    relative_parts = path.relative_to(helper_host.root).parts
    component = helper_host.root.joinpath(*relative_parts[: component_index + 1])

    result = helper_host.run(FAKE_SYMLINK_COMPONENT=str(component))

    _assert_failure(result, "path_canonical_mismatch", {}, helper_host)


@pytest.mark.parametrize(
    ("stage", "error_code"),
    [
        ("bind-final", "mount_bind_failed"),
        ("remount-final", "mount_remount_failed"),
        ("bind-probe", "mount_bind_failed"),
        ("remount-probe", "mount_remount_failed"),
    ],
)
def test_partial_failure_rolls_back_invocation_mounts_in_reverse_order(
    helper_host: HelperHost, stage: str, error_code: str
) -> None:
    result = helper_host.run(FAKE_MOUNT_FAIL_AT=stage)

    _assert_failure(result, error_code, {}, helper_host)
    unmounts = [call for call in helper_host.calls() if call.startswith("umount ")]
    expected = {
        "bind-final": [],
        "remount-final": [f"umount -- {_call_path(helper_host.final_target)}"],
        "bind-probe": [f"umount -- {_call_path(helper_host.final_target)}"],
        "remount-probe": [
            f"umount -- {_call_path(helper_host.probe_target)}",
            f"umount -- {_call_path(helper_host.final_target)}",
        ],
    }
    assert unmounts == expected[stage]


def test_failure_preserves_preexisting_verified_mount(helper_host: HelperHost) -> None:
    helper_host.set_mount(helper_host.final_source, helper_host.final_target)
    existing = helper_host.mounts()

    result = helper_host.run(FAKE_MOUNT_FAIL_AT="remount-probe")

    _assert_failure(result, "mount_remount_failed", existing, helper_host)
    assert [call for call in helper_host.calls() if call.startswith("umount ")] == [
        f"umount -- {_call_path(helper_host.probe_target)}"
    ]
