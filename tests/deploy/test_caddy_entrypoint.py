from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "deploy" / "caddy" / "caddy-entrypoint.sh"
BASH = shutil.which("bash") or "D:/Git/usr/bin/bash.exe"


def _shell_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if len(value) > 2 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _write_tool(path: Path, body: str) -> None:
    path.write_bytes(("#!/bin/sh\nset -eu\n" + body).encode("utf-8"))
    path.chmod(0o755)


def _fixture_root(tmp_path: Path, *, final_options: str = "ro,nosymfollow", probe_options: str = "ro,nosymfollow") -> Path:
    final = tmp_path / "srv" / "eurith" / "releases" / "android" / "sha256"
    probe = tmp_path / "run" / "eurith-release-view-probe"
    mountinfo = tmp_path / "proc" / "self" / "mountinfo"
    tools = tmp_path / "test-bin"
    final.mkdir(parents=True)
    probe.mkdir(parents=True)
    mountinfo.parent.mkdir(parents=True)
    tools.mkdir()
    (probe / "regular").write_text("probe\n", encoding="utf-8")
    (probe / "external").write_text("test-double\n", encoding="utf-8")
    mountinfo.write_text(
        f"11 1 0:1 / FINAL_MOUNT {final_options} - bind source rw\n"
        f"12 1 0:2 / PROBE_MOUNT {probe_options} - bind source rw\n",
        encoding="utf-8",
    )
    _write_tool(
        tools / "head",
        'for arg in "$@"; do last=$arg; done\n'
        'case "$last" in\n'
        '  */external) exit "${FAKE_EXTERNAL_READ_STATUS:-1}" ;;\n'
        '  */regular) exit "${FAKE_REGULAR_READ_STATUS:-0}" ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n',
    )
    _write_tool(
        tools / "readlink",
        'printf "%s\\n" "${FAKE_EXTERNAL_TARGET:-/etc/passwd}"\n',
    )
    _write_tool(
        tools / "caddy",
        'printf "%s\\n" "$*" > "${CADDY_EXEC_MARKER:?}"\n',
    )
    return tmp_path


def _run(root: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    marker = root / "caddy-exec-marker"
    env = os.environ.copy()
    env.update(overrides)
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    env["CADDY_EXEC_MARKER"] = _shell_path(marker)
    return subprocess.run(
        [BASH, _shell_path(ENTRYPOINT), "--test-root", _shell_path(root)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_entrypoint_checks_mounts_and_probes_before_execing_caddy(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    result = _run(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "release_view_gate=passed\n"
    assert (root / "caddy-exec-marker").read_text(encoding="utf-8").strip() == (
        "run --config /etc/caddy/Caddyfile --adapter caddyfile"
    )


@pytest.mark.parametrize(
    ("final_options", "probe_options", "overrides"),
    [
        ("nosymfollow", "ro,nosymfollow", {}),
        ("ro", "ro,nosymfollow", {}),
        ("ro,nosymfollow", "nosymfollow", {}),
        ("ro,nosymfollow", "ro", {}),
        ("ro,nosymfollow", "ro,nosymfollow", {"FAKE_REGULAR_READ_STATUS": "1"}),
        ("ro,nosymfollow", "ro,nosymfollow", {"FAKE_EXTERNAL_READ_STATUS": "0"}),
        ("ro,nosymfollow", "ro,nosymfollow", {"FAKE_EXTERNAL_TARGET": "/tmp/other"}),
    ],
)
def test_entrypoint_failure_never_execs_caddy(
    tmp_path: Path,
    final_options: str,
    probe_options: str,
    overrides: dict[str, str],
) -> None:
    root = _fixture_root(
        tmp_path,
        final_options=final_options,
        probe_options=probe_options,
    )
    result = _run(root, **overrides)
    assert result.returncode != 0
    assert result.stderr == "release_view_gate=failed\n"
    assert not (root / "caddy-exec-marker").exists()


def test_entrypoint_rechecks_on_every_invocation(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    assert _run(root).returncode == 0
    (root / "caddy-exec-marker").unlink()
    mountinfo = root / "proc" / "self" / "mountinfo"
    mountinfo.write_text(
        mountinfo.read_text(encoding="utf-8").replace("ro,nosymfollow", "ro"),
        encoding="utf-8",
    )
    result = _run(root)
    assert result.returncode != 0
    assert not (root / "caddy-exec-marker").exists()
