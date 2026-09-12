from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "deploy.sh"
CANARY = ROOT / "deploy" / "canary-release-delivery.sh"
HEADER_GATE = ROOT / "deploy" / "http-header-gate.py"
PUBLISH_GATE = ROOT / "deploy" / "publish-mobile-gate.py"
MIGRATION_MANIFEST = ROOT / "deploy" / "migration-path-manifest.py"
MIGRATION_APPROVAL = ROOT / "deploy" / "validate-migration-approval.py"
REHEARSAL_PROBE = ROOT / "deploy" / "rehearse-release-db.py"
REHEARSAL_INPUTS = ROOT / "deploy" / "prepare-rehearsal-inputs.py"
README = ROOT / "deploy" / "README.md"
CHECKLIST = ROOT / "docs" / "releases" / "update-center-backend-checklist.md"
BASH = shutil.which("bash") or "D:/Git/usr/bin/bash.exe"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _shell(path: Path) -> str:
    value = path.resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if len(value) > 2 and value[1] == ":" else value


def _wrapper(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _boot_gate_functions() -> str:
    script = _text(DEPLOY)
    marker = "verify_installed_boot_assets() {"
    assert marker in script, "deploy.sh does not define the boot-mount deployment gate"
    start = script.index("verify_boot_asset() {")
    end = script.index("\nbuild_target() {", start)
    return f'source "{_shell(ROOT / "deploy/lib/release_common.sh")}"\n' + script[start:end]


def _run_boot_gate_harness(tmp_path: Path, case: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    asset_root = tmp_path / "assets"
    asset_dir = asset_root / "deploy" / "systemd"; asset_dir.mkdir(parents=True)
    installed_dir = tmp_path / "installed"; installed_dir.mkdir()
    evidence_file = tmp_path / "evidence.env"

    helper_body = '''#!/usr/bin/env bash
case "$BOOT_GATE_CASE" in
  final_source_drift) printf 'error=mount_source_mismatch path=/opt/eurith/release-caddy-view/android/sha256\\n' >&2; exit 1 ;;
  final_target_drift) printf 'error=mount_target_mismatch path=/opt/eurith/release-caddy-view/android/sha256\\n' >&2; exit 1 ;;
  final_options_drift) printf 'error=mount_readonly_missing path=/opt/eurith/release-caddy-view/android/sha256\\n' >&2; exit 1 ;;
  probe_source_drift) printf 'error=mount_source_mismatch path=/opt/eurith/release-caddy-view/.probe\\n' >&2; exit 1 ;;
  probe_target_drift) printf 'error=mount_target_mismatch path=/opt/eurith/release-caddy-view/.probe\\n' >&2; exit 1 ;;
  probe_options_drift) printf 'error=mount_nosymfollow_missing path=/opt/eurith/release-caddy-view/.probe\\n' >&2; exit 1 ;;
  helper_nonzero) exit 23 ;;
  success) printf 'boot_mounts=verified\\n' ;;
  *) printf 'boot_mounts=verified\\n' ;;
esac
'''
    assets = {
        "eurith-release-views": helper_body,
        "eurith-release-views.service": "[Service]\nType=oneshot\n",
        "docker-eurith-release-views.conf": "[Unit]\nAfter=eurith-release-views.service\n",
    }
    installed: dict[str, Path] = {}
    for name, content in assets.items():
        (asset_dir / name).write_text(content, encoding="ascii", newline="\n")
        destination = installed_dir / name
        destination.write_text(content, encoding="ascii", newline="\n")
        destination.chmod(0o755 if name == "eurith-release-views" else 0o644)
        installed[name] = destination

    corrupt = {
        "helper_hash": "eurith-release-views",
        "unit_hash": "eurith-release-views.service",
        "drop_in_hash": "docker-eurith-release-views.conf",
    }.get(case)
    if corrupt is not None:
        installed[corrupt].write_text("corrupt\n", encoding="ascii")
    if case == "helper_type":
        installed["eurith-release-views"].unlink()
        installed["eurith-release-views"].mkdir()

    _wrapper(bin_dir, "systemctl", '''
if [[ "$1" == show ]]; then
  exec "$FAKE_PYTHON" "$FAKE_SYSTEMD_SHOW" "$2" "$BOOT_GATE_CASE" "$FAKE_UNIT" "$FAKE_DROP_IN"
fi
case "$*" in
  "is-enabled --quiet eurith-release-views.service")
    [[ "$BOOT_GATE_CASE" != unit_disabled ]] ;;
  "is-enabled eurith-release-views.service")
    case "$BOOT_GATE_CASE" in
      unit_disabled) printf 'disabled\n'; exit 1 ;;
      unit_enabled_runtime) printf 'enabled-runtime\n' ;;
      *) printf 'enabled\n' ;;
    esac ;;
  "is-active --quiet eurith-release-views.service")
    [[ "$BOOT_GATE_CASE" != unit_inactive && "$BOOT_GATE_CASE" != unit_failed ]] ;;
  *) exit 91 ;;
esac
''')
    _wrapper(bin_dir, "sha256sum", '''
path="${@: -1}"
is_source=0; [[ "$path" == *"/assets/deploy/systemd/"* ]] && is_source=1
case "$BOOT_GATE_CASE" in
  source_read_failure) [[ "$is_source" == 0 ]] || exit 41 ;;
  installed_read_failure) [[ "$is_source" == 1 ]] || exit 42 ;;
  both_read_failure) exit 43 ;;
  source_hash_malformed) if [[ "$is_source" == 1 ]]; then printf 'INVALID  %s\n' "$path"; exit 0; fi ;;
  installed_hash_malformed) if [[ "$is_source" == 0 ]]; then printf 'INVALID  %s\n' "$path"; exit 0; fi ;;
  both_hash_malformed) printf 'INVALID  %s\n' "$path"; exit 0 ;;
esac
exec /usr/bin/sha256sum "$@"
''')
    _wrapper(bin_dir, "stat", '''
if [[ "$1" == -c && "$2" == "%a:%u:%g" ]]; then
  path="${@: -1}"
  case "$BOOT_GATE_CASE:$path" in
    helper_mode:*eurith-release-views) printf '700:0:0\\n' ;;
    unit_owner:*eurith-release-views.service) printf '644:1000:0\\n' ;;
    drop_in_owner:*docker-eurith-release-views.conf) printf '644:0:1000\\n' ;;
    *:*eurith-release-views) printf '755:0:0\\n' ;;
    *:*eurith-release-views.service|*:*docker-eurith-release-views.conf) printf '644:0:0\\n' ;;
    *) exit 92 ;;
  esac
else
  exec /usr/bin/stat "$@"
fi
''')
    runner = tmp_path / "runner.sh"
    runner.write_text(
        "#!/usr/bin/env bash\nset -Eeuo pipefail\n"
        "die() { printf 'error=%s\\n' \"$1\" >&2; exit 1; }\n"
        "evidence() { printf '%s=%s\\n' \"$1\" \"$2\" >>\"$EVIDENCE_FILE\"; }\n"
        "sha256_file() { sha256sum -- \"$1\" | awk '{print $1}'; }\n"
        + _boot_gate_functions()
        + f"\nEURITH_DEPLOY_ASSET_ROOT='{_shell(asset_root)}'\n"
        + f"BOOT_HELPER_DESTINATION='{_shell(installed['eurith-release-views'])}'\n"
        + f"BOOT_UNIT_DESTINATION='{_shell(installed['eurith-release-views.service'])}'\n"
        + f"DOCKER_DROP_IN_DESTINATION='{_shell(installed['docker-eurith-release-views.conf'])}'\n"
        + "verify_boot_mount_gate\nevidence backend_gate passed\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    env = os.environ.copy(); env.update({
        "PATH": _shell(bin_dir) + ":/usr/bin:/bin",
        "BOOT_GATE_CASE": case,
        "EVIDENCE_FILE": _shell(evidence_file),
        "FAKE_PYTHON": Path(sys.executable).as_posix(),
        "FAKE_SYSTEMD_SHOW": (ROOT / "tests/deploy/fake_systemd_show.py").as_posix(),
        "FAKE_UNIT": _shell(installed["eurith-release-views.service"]),
        "FAKE_DROP_IN": _shell(installed["docker-eurith-release-views.conf"]),
    })
    completed = subprocess.run(
        [BASH, _shell(runner)], env=env, capture_output=True, text=True, timeout=15,
    )
    evidence = evidence_file.read_text(encoding="ascii").splitlines() if evidence_file.exists() else []
    return completed, evidence


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("helper_hash", "boot_mount_helper_bytes_mismatch"),
        ("unit_hash", "boot_mount_unit_bytes_mismatch"),
        ("drop_in_hash", "boot_mount_docker_drop_in_bytes_mismatch"),
        ("helper_type", "boot_mount_helper_metadata_invalid"),
        ("helper_mode", "boot_mount_helper_metadata_invalid"),
        ("unit_owner", "boot_mount_unit_metadata_invalid"),
        ("drop_in_owner", "boot_mount_docker_drop_in_metadata_invalid"),
        ("unit_disabled", "boot_mount_unit_not_enabled"),
        ("unit_enabled_runtime", "boot_mount_unit_not_enabled"),
        ("unit_inactive", "boot_mount_unit_not_active"),
        ("unit_failed", "boot_mount_unit_not_active"),
        ("helper_nonzero", "boot_mount_runtime_verification_failed"),
        ("source_read_failure", "boot_mount_target_asset_hash_unreadable"),
        ("installed_read_failure", "boot_mount_installed_asset_hash_unreadable"),
        ("both_read_failure", "boot_mount_target_asset_hash_unreadable"),
        ("source_hash_malformed", "boot_mount_target_asset_hash_invalid"),
        ("installed_hash_malformed", "boot_mount_installed_asset_hash_invalid"),
        ("both_hash_malformed", "boot_mount_target_asset_hash_invalid"),
        ("final_source_drift", "mount_source_mismatch"),
        ("final_target_drift", "mount_target_mismatch"),
        ("final_options_drift", "mount_readonly_missing"),
        ("probe_source_drift", "mount_source_mismatch"),
        ("probe_target_drift", "mount_target_mismatch"),
        ("probe_options_drift", "mount_nosymfollow_missing"),
    ],
)
def test_boot_mount_gate_rejects_each_failed_invariant(
    tmp_path: Path, case: str, expected_error: str
) -> None:
    result, evidence = _run_boot_gate_harness(tmp_path, case)
    assert result.returncode != 0
    assert f"error={expected_error}" in result.stderr
    assert "backend_gate=passed" not in evidence


def test_boot_mount_gate_emits_live_proofs_before_single_backend_pass(tmp_path: Path) -> None:
    result, evidence = _run_boot_gate_harness(tmp_path, "success")
    assert result.returncode == 0, result.stderr
    assert evidence == [
        "boot_mount_assets=verified",
        "boot_mount_unit=enabled",
        "boot_mount_runtime=verified",
        "backend_gate=passed",
    ]


def test_boot_mount_gate_accepts_real_systemd_empty_struct_array_output(tmp_path: Path) -> None:
    result, evidence = _run_boot_gate_harness(tmp_path, "systemd_empty_arrays")
    assert result.returncode == 0, result.stderr
    assert evidence.count("backend_gate=passed") == 1


@pytest.mark.parametrize("case", [
    "release_dropin", "release_transient", "release_fragment", "release_stale",
    "release_exec", "release_environment", "release_environment_file",
    "release_namespace", "release_root", "release_order", "release_mount_dependency",
    "docker_dependency", "docker_order", "docker_dropin", "docker_transient",
])
def test_effective_systemd_override_never_allows_backend_gate(tmp_path: Path, case: str) -> None:
    result, evidence = _run_boot_gate_harness(tmp_path, case)
    assert result.returncode != 0
    assert "backend_gate=passed" not in evidence
    assert "error=boot_systemd_" in result.stderr


def _function(script: str, name: str, next_name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index(f"\n{next_name}() {{", start)
    return script[start:end]


def _run_real_deploy_boot_path(
    tmp_path: Path, final_case: str
) -> tuple[subprocess.CompletedProcess[str], list[str], bool]:
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    state_dir = tmp_path / "state"; state_dir.mkdir()
    script_dir = tmp_path / "deploy"; script_dir.mkdir()
    asset_root = tmp_path / "assets"
    asset_dir = asset_root / "deploy" / "systemd"; asset_dir.mkdir(parents=True)
    installed_dir = tmp_path / "installed"; installed_dir.mkdir()
    evidence_dir = tmp_path / "evidence"; evidence_dir.mkdir()
    evidence_file = evidence_dir / "deploy.env"
    evidence_file.write_text("", encoding="ascii")
    mobile_gate = evidence_dir / "mobile.env"
    calls = state_dir / "calls.log"; calls.write_text("", encoding="ascii")

    final_source = tmp_path / "mounts" / "final-source"
    final_target = tmp_path / "mounts" / "final-target"
    probe_source = tmp_path / "mounts" / "probe-source"
    probe_target = tmp_path / "mounts" / "probe-target"
    wrong_target = tmp_path / "mounts" / "wrong-target"
    for path in (final_source, final_target, probe_source, probe_target, wrong_target):
        path.mkdir(parents=True, exist_ok=True)

    helper = _text(ROOT / "deploy" / "systemd" / "eurith-release-views")
    replacements = {
        "/opt/eurith/releases/android/sha256": _shell(final_source),
        "/opt/eurith/release-caddy-view/android/sha256": _shell(final_target),
        "/opt/eurith/release-caddy-probe-source": _shell(probe_source),
        "/opt/eurith/release-caddy-view/.probe": _shell(probe_target),
    }
    for production, rendered in sorted(replacements.items(), key=lambda item: -len(item[0])):
        helper = helper.replace(production, rendered)
    assets = {
        "eurith-release-views": helper,
        "eurith-release-views.service": "[Service]\nType=oneshot\n",
        "docker-eurith-release-views.conf": "[Unit]\nAfter=eurith-release-views.service\n",
    }
    installed: dict[str, Path] = {}
    for name, content in assets.items():
        (asset_dir / name).write_text(content, encoding="utf-8", newline="\n")
        destination = installed_dir / name
        destination.write_text(content, encoding="utf-8", newline="\n")
        destination.chmod(0o755 if name == "eurith-release-views" else 0o644)
        installed[name] = destination

    _wrapper(script_dir, "provision-release-host.sh", "exit 0")
    _wrapper(bin_dir, "systemctl", '''
if [[ "$1" == show ]]; then
  effective_case=success
  [[ "$BOOT_GATE_PHASE" != final ]] || effective_case="${FINAL_GATE_CASE#final_}"
  exec "$FAKE_PYTHON" "$FAKE_SYSTEMD_SHOW" "$2" "$effective_case" "$FAKE_UNIT" "$FAKE_DROP_IN"
fi
case "$*" in
  "is-enabled --quiet eurith-release-views.service")
    [[ "$BOOT_GATE_PHASE" != final || "$FINAL_GATE_CASE" != final_unit_disabled ]] ;;
  "is-enabled eurith-release-views.service")
    if [[ "$BOOT_GATE_PHASE" == final ]]; then
      case "$FINAL_GATE_CASE" in
        final_unit_disabled) printf 'disabled\n'; exit 1 ;;
        final_unit_enabled_runtime) printf 'enabled-runtime\n' ;;
        *) printf 'enabled\n' ;;
      esac
    else printf 'enabled\n'; fi ;;
  "is-active --quiet eurith-release-views.service")
    [[ "$BOOT_GATE_PHASE" != final || ( "$FINAL_GATE_CASE" != final_unit_inactive && "$FINAL_GATE_CASE" != final_unit_failed ) ]] ;;
  *) exit 91 ;;
esac
''')
    _wrapper(bin_dir, "sha256sum", '''
path="${@: -1}"
if [[ "$BOOT_GATE_PHASE" == final && "$FINAL_GATE_CASE" == final_helper_hash && "$path" == *"/installed/eurith-release-views" ]]; then
  printf '%064d  %s\n' 0 "$path"
  exit 0
fi
exec /usr/bin/sha256sum "$@"
''')
    _wrapper(bin_dir, "readlink", '''
if [[ "$BOOT_GATE_PHASE" == final && "$FINAL_GATE_CASE" == final_helper_nonzero ]]; then exit 1; fi
[[ -e "${@: -1}" ]] || exit 1
printf "%s\n" "${@: -1}"
''')
    _wrapper(bin_dir, "findmnt", '''
target="${@: -1}"; kind=probe; [[ "$target" == "$FAKE_FINAL_TARGET" ]] && kind=final
case "$*" in
  *" -o TARGET "*)
    if [[ "$BOOT_GATE_PHASE" == final && "$FINAL_GATE_CASE" == "final_${kind}_target_drift" ]]; then printf '%s\n' "$FAKE_WRONG_TARGET"; else printf '%s\n' "$target"; fi ;;
  *" -o SOURCE "*) printf '/dev/fake\n' ;;
  *" -o VFS-OPTIONS "*)
    if [[ "$BOOT_GATE_PHASE" == final && "$FINAL_GATE_CASE" == "final_${kind}_options_drift" ]]; then printf 'rw,nosuid\n'; else printf 'ro,nosymfollow,nosuid\n'; fi ;;
  *) exit 92 ;;
esac
''')
    _wrapper(bin_dir, "stat", '''
path="${@: -1}"
if [[ "$*" == *"%a:%u:%g"* ]]; then
  case "$path" in *eurith-release-views) printf '755:0:0\n' ;; *) printf '644:0:0\n' ;; esac
elif [[ "$*" == *"%a:%u"* ]]; then
  printf '755:0\n'
elif [[ "$*" == *"%d:%i"* ]]; then
  if [[ "$BOOT_GATE_PHASE" == final && "$FINAL_GATE_CASE" == final_final_source_drift && "$path" == "$FAKE_FINAL_TARGET" ]]; then printf 'wrong-final\n'
  elif [[ "$BOOT_GATE_PHASE" == final && "$FINAL_GATE_CASE" == final_probe_source_drift && "$path" == "$FAKE_PROBE_TARGET" ]]; then printf 'wrong-probe\n'
  elif [[ "$path" == "$FAKE_FINAL_SOURCE" || "$path" == "$FAKE_FINAL_TARGET" ]]; then printf 'final-identity\n'
  else printf 'probe-identity\n'; fi
else exit 93; fi
''')
    _wrapper(bin_dir, "mount", "exit 94")
    _wrapper(bin_dir, "umount", "exit 0")
    _wrapper(bin_dir, "docker", '''
case "$*" in
  *" build api") exit 0 ;;
  *" run --rm --no-deps api alembic heads") printf 'head\n' ;;
  *) exit 95 ;;
esac
''')
    _wrapper(bin_dir, "python3", '''
printf 'python3 %s\n' "$*" >>"$FAKE_CALLS"
: >"$3"
''')
    _wrapper(bin_dir, "date", "exec /usr/bin/date \"$@\"")

    deploy_script = _text(DEPLOY)
    gate_functions = deploy_script[
        deploy_script.index("verify_boot_asset() {") : deploy_script.index("\nrehearse_migration_compatibility() {")
    ]
    write_gate = _function(deploy_script, "write_mobile_gate", "on_exit")
    runner = tmp_path / "runner.sh"
    runner.write_text(
        "#!/usr/bin/env bash\nset -Eeuo pipefail\n"
        "die() { printf 'error=%s\\n' \"$1\" >&2; exit 1; }\n"
        "evidence() { printf '%s=%s\\n' \"$1\" \"$2\" >>\"$EVIDENCE_DIR/deploy.env\"; }\n"
        "sha256_file() { sha256sum -- \"$1\" | awk '{print $1}'; }\n"
        + f'source "{_shell(ROOT / "deploy/lib/release_common.sh")}"\n'
        + gate_functions + "\n" + write_gate
        + f"\nSCRIPT_DIR='{_shell(script_dir)}'\nEURITH_DEPLOY_ASSET_ROOT='{_shell(asset_root)}'\n"
        + f"BOOT_HELPER_DESTINATION='{_shell(installed['eurith-release-views'])}'\n"
        + f"BOOT_UNIT_DESTINATION='{_shell(installed['eurith-release-views.service'])}'\n"
        + f"DOCKER_DROP_IN_DESTINATION='{_shell(installed['docker-eurith-release-views.conf'])}'\n"
        + f"EVIDENCE_DIR='{_shell(evidence_dir)}'\nMOBILE_GATE_FILE='{_shell(mobile_gate)}'\n"
        + "EURITH_SECRET_SOURCE_DIR=/fake/secrets\nEURITH_BASE_COMPOSE=/fake/base-compose.yml\n"
        + "RELEASE_OVERLAY=/fake/release-overlay.yml\n"
        + "MOBILE_GATE_PUBLISHER=fake-publisher\nTARGET_SHA=" + "a" * 40 + "\nMOBILE_CANDIDATE_SHA=" + "b" * 40 + "\n"
        + "EXPECTED_ALEMBIC_HEAD=head\ncompose=(docker compose)\n"
        + "export BOOT_GATE_PHASE=initial\nbuild_target\nexport BOOT_GATE_PHASE=final\nwrite_mobile_gate\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    env = os.environ.copy(); env.update({
        "PATH": _shell(bin_dir) + ":/usr/bin:/bin",
        "FINAL_GATE_CASE": final_case,
        "FAKE_PYTHON": Path(sys.executable).as_posix(),
        "FAKE_SYSTEMD_SHOW": (ROOT / "tests/deploy/fake_systemd_show.py").as_posix(),
        "FAKE_UNIT": _shell(installed["eurith-release-views.service"]),
        "FAKE_DROP_IN": _shell(installed["docker-eurith-release-views.conf"]),
        "FAKE_CALLS": _shell(calls),
        "FAKE_FINAL_SOURCE": _shell(final_source),
        "FAKE_FINAL_TARGET": _shell(final_target),
        "FAKE_PROBE_SOURCE": _shell(probe_source),
        "FAKE_PROBE_TARGET": _shell(probe_target),
        "FAKE_WRONG_TARGET": _shell(wrong_target),
    })
    completed = subprocess.run([BASH, _shell(runner)], env=env, capture_output=True, text=True, timeout=15)
    return completed, evidence_file.read_text(encoding="ascii").splitlines(), mobile_gate.exists()


@pytest.mark.parametrize(
    "final_case",
    [
        "final_helper_hash", "final_helper_nonzero", "final_unit_disabled",
        "final_unit_enabled_runtime", "final_unit_inactive", "final_unit_failed",
        "final_final_source_drift", "final_final_target_drift", "final_final_options_drift",
        "final_probe_source_drift", "final_probe_target_drift", "final_probe_options_drift",
        "final_release_dropin", "final_release_environment", "final_docker_dependency",
    ],
)
def test_real_final_publication_path_rechecks_changed_boot_state(
    tmp_path: Path, final_case: str
) -> None:
    result, evidence, gate_exists = _run_real_deploy_boot_path(tmp_path, final_case)
    assert result.returncode != 0
    assert "build_status=passed" in evidence
    assert "backend_gate=passed" not in evidence
    assert "backend_gate=passed" not in result.stdout
    assert not gate_exists


def test_real_build_and_final_publication_path_succeeds_without_state_change(tmp_path: Path) -> None:
    result, evidence, gate_exists = _run_real_deploy_boot_path(tmp_path, "success")
    assert result.returncode == 0, result.stderr
    assert evidence.count("boot_mount_assets=verified") == 2
    assert evidence.count("boot_mount_unit=enabled") == 2
    assert evidence.count("boot_mount_runtime=verified") == 2
    assert evidence.count("backend_gate=passed") == 1
    assert gate_exists


def _rollback_function() -> str:
    script = _text(DEPLOY)
    start = script.index("rollback_infrastructure() {")
    end = script.index("\nswitch_api_and_caddy() {", start)
    return script[start:end]


def _run_rollback_harness(tmp_path: Path, case: str, prior_caddy: bool) -> list[str]:
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    state_dir = tmp_path / "state"; state_dir.mkdir()
    evidence_file = tmp_path / "evidence.env"
    source = tmp_path / "source"; source.mkdir()
    overlay = tmp_path / "release.yml"; overlay.write_text("services: {}\n", encoding="ascii")
    base = tmp_path / "compose.yml"; base.write_text("services: {}\n", encoding="ascii")
    deploy_env = tmp_path / "deploy.env"; deploy_env.write_text("X=1\n", encoding="ascii")
    asset_root = tmp_path / "assets"; asset_root.mkdir()
    rollback_sha = "d" * 40
    built_image = "sha256:" + "a" * 64
    wrong_image = "sha256:" + "b" * 64
    old_caddy_image = "sha256:" + "c" * 64

    _wrapper(bin_dir, "git", f'''case "$*" in
  *"checkout --detach {rollback_sha}") exit 0 ;;
  *"rev-parse HEAD") printf "%s\\n" "{rollback_sha}" ;;
  *"status --porcelain") exit 0 ;;
  *) exit 97 ;;
esac''')
    _wrapper(bin_dir, "curl", '''
if [[ "$ROLLBACK_CASE" == readiness_timeout ]]; then printf 503
else : >"$ROLLBACK_STATE/ready"; printf 200; fi
''')
    _wrapper(bin_dir, "sleep", ":")
    _wrapper(bin_dir, "docker", f'''next_count() {{
  local name="$1" path="$ROLLBACK_STATE/$1" value=0
  [[ ! -f "$path" ]] || value="$(<"$path")"
  printf "%s" "$((value + 1))" >"$path"
  printf "%s" "$value"
}}
if [[ "$1" == compose ]]; then
  case "$*" in
    *" rm -f caddy") exit 0 ;;
    *" build api") exit 0 ;;
    *" config --images api") printf "eurith-api:rollback\\n" ;;
    *" up -d "*) [[ "$ROLLBACK_CASE" != up_failure ]] ;;
    *" ps -q api")
      [[ "$ROLLBACK_CASE" != up_failure ]] || exit 0
      count="$(next_count api_ps)"
      if [[ "$ROLLBACK_CASE" == container_swap && "$count" -gt 0 ]]; then printf "api-swap\\n"; else printf "api-id\\n"; fi ;;
    *" ps -q caddy"|*" ps -a -q caddy")
      if [[ "$PRIOR_CADDY" == 0 ]]; then
        [[ "$ROLLBACK_CASE" == caddy_unexpected && -f "$ROLLBACK_STATE/ready" ]] && printf "caddy-unexpected\\n"
        exit 0
      fi
      [[ "$ROLLBACK_CASE" != up_failure ]] || exit 0
      if [[ "$ROLLBACK_CASE" == caddy_swap_after && -f "$ROLLBACK_STATE/ready" ]]; then printf "caddy-swap\\n"
      elif [[ "$ROLLBACK_CASE" == caddy_missing_after && -f "$ROLLBACK_STATE/ready" ]]; then exit 0
      else printf "caddy-id\\n"; fi ;;
    *) exit 96 ;;
  esac
elif [[ "$1 $2" == "image inspect" ]]; then
  printf "%s\\n" "{built_image}"
elif [[ "$1" == inspect ]]; then
  container="$2"; format="$4"
  if [[ "$container" == caddy-* ]]; then
    case "$format" in
      "{{{{.Image}}}}")
        if [[ "$ROLLBACK_CASE" == caddy_image_after && -f "$ROLLBACK_STATE/ready" ]]; then printf "%s\\n" "{wrong_image}"
        else printf "%s\\n" "{old_caddy_image}"; fi ;;
      "{{{{.State.Status}}}}")
        if [[ "$ROLLBACK_CASE" == caddy_crash_before || ( "$ROLLBACK_CASE" == caddy_crash_after && -f "$ROLLBACK_STATE/ready" ) ]]; then printf "exited\\n"
        else printf "running\\n"; fi ;;
      "{{{{.RestartCount}}}}")
        if [[ "$ROLLBACK_CASE" == caddy_restart_invalid ]]; then printf "unknown\\n"
        elif [[ "$ROLLBACK_CASE" == caddy_restart_after && -f "$ROLLBACK_STATE/ready" ]]; then printf "3\\n"
        else printf "2\\n"; fi ;;
      *) exit 95 ;;
    esac
    exit 0
  fi
  case "$format" in
    "{{{{.Image}}}}")
      if [[ "$container" == caddy-id ]]; then printf "%s\\n" "{old_caddy_image}"
      elif [[ "$ROLLBACK_CASE" == wrong_image ]]; then printf "%s\\n" "{wrong_image}"
      else printf "%s\\n" "{built_image}"; fi ;;
    "{{{{.State.Status}}}}") [[ "$ROLLBACK_CASE" == not_running ]] && printf "exited\\n" || printf "running\\n" ;;
    "{{{{.RestartCount}}}}")
      count="$(next_count restart)"
      if [[ "$ROLLBACK_CASE" == restart_before ]]; then printf "1\\n"
      elif [[ "$ROLLBACK_CASE" == restart_after && "$count" -gt 0 ]]; then printf "1\\n"
      else printf "0\\n"; fi ;;
    *) exit 95 ;;
  esac
else
  exit 94
fi''')
    runner = tmp_path / "runner.sh"
    runner.write_text(
        "#!/usr/bin/env bash\nset -u\n"
        "evidence() { printf '%s=%s\\n' \"$1\" \"$2\" >>\"$EVIDENCE_FILE\"; }\n"
        + _rollback_function()
        + "\ncompose=(docker compose)\n"
        + "ROLLBACK_ATTEMPTED=0\nSWITCH_ATTEMPTED=1\nMIGRATION_ATTEMPTED=1\nMIGRATION_STATE=applied\n"
        + "SCHEMA_ROLLBACK_COMPATIBLE=1\nROLLBACK_EVIDENCE_WRITTEN=0\n"
        + f"PRIOR_CADDY_PRESENT={'1' if prior_caddy else '0'}\n"
        + f"OLD_CADDY_IMAGE_ID={old_caddy_image}\nROLLBACK_SHA={rollback_sha}\n"
        + f"SOURCE_DIR='{_shell(source)}'\nEURITH_DEPLOY_ASSET_ROOT='{_shell(asset_root)}'\n"
        + f"DEPLOY_ENV='{_shell(deploy_env)}'\nEURITH_BASE_COMPOSE='{_shell(base)}'\nRELEASE_OVERLAY='{_shell(overlay)}'\n"
        + "PUBLIC_API_BASE=https://example.invalid\n"
        + "set +e\nrollback_infrastructure\nrc=$?\nset -e\nprintf 'return_code=%s\\n' \"$rc\"\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    env = os.environ.copy(); env.update({
        "PATH": _shell(bin_dir) + ":/usr/bin:/bin",
        "ROLLBACK_CASE": case,
        "PRIOR_CADDY": "1" if prior_caddy else "0",
        "ROLLBACK_STATE": _shell(state_dir),
        "EVIDENCE_FILE": _shell(evidence_file),
    })
    completed = subprocess.run([BASH, _shell(runner)], env=env, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert "return_code=1" in completed.stdout
    return evidence_file.read_text(encoding="ascii").splitlines()


def test_deploy_requires_two_arguments_plus_protected_exact_rollback_sha_and_remote_containment() -> None:
    script = _text(DEPLOY)
    assert "[[ $# == 2 ]]" in script
    assert 'require_full_sha "$TARGET_SHA"' in script
    assert 'require_full_sha "$MOBILE_CANDIDATE_SHA"' in script
    assert 'require_full_sha "$ROLLBACK_SHA"' in script
    assert 'ROLLBACK_SHA="${EURITH_ROLLBACK_SHA:-}"' in script
    assert "git status --porcelain" in script
    assert 'git merge-base --is-ancestor "$ROLLBACK_SHA" "$TARGET_SHA"' in script
    assert 'git merge-base --is-ancestor "$ROLLBACK_SHA" "$APPROVED_REMOTE_REF"' in script
    assert "APPROVED_REMOTE_REF" in script
    assert "deploy_asset_root_sha_mismatch" in script


def test_rollback_candidate_rehearsal_and_recovery_are_exact_and_distinct_from_incumbent() -> None:
    script = _text(DEPLOY)
    rehearsal = script[script.index("rehearse_migration_compatibility()") : script.index("apply_migration_once()")]
    rollback = script[script.index("rollback_infrastructure()") : script.index("switch_api_and_caddy()")]
    assert 'checkout --detach "$ROLLBACK_SHA"' in rehearsal
    assert 'rev-parse HEAD)" == "$ROLLBACK_SHA"' in rehearsal
    assert 'EURITH_RUNTIME_SOURCE_ROOT="$SOURCE_DIR"' in rehearsal
    assert 'EURITH_RUNTIME_ASSET_ROOT="$EURITH_DEPLOY_ASSET_ROOT"' in rehearsal
    assert '-f "$RELEASE_OVERLAY"' in rehearsal
    assert 'rollback_heads="$(' in rehearsal
    assert '[[ "$rollback_heads" == "$EXPECTED_ALEMBIC_HEAD" ]]' in rehearsal
    assert "rollback_candidate_compatibility_rehearsal_failed" in rehearsal
    assert 'checkout --detach "$ROLLBACK_SHA"' in rollback
    assert 'rev-parse HEAD 2>/dev/null)" != "$ROLLBACK_SHA"' in rollback
    assert 'EURITH_RUNTIME_SOURCE_ROOT="$SOURCE_DIR"' in rollback
    assert 'EURITH_RUNTIME_ASSET_ROOT="$EURITH_DEPLOY_ASSET_ROOT"' in rollback
    assert 'rollback_compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$RELEASE_OVERLAY")' in rollback
    assert 'evidence rollback_backend_sha "$ROLLBACK_SHA"' in script
    assert '"$ROLLBACK_SHA" == "$OLD_COMMIT"' not in script
    assert script.index("CURRENT_STAGE=capture_prior_runtime") < script.index("CURRENT_STAGE=apply_migration_once")
    capture = script[script.index("capture_prior_runtime()") : script.index("rollback_infrastructure()")]
    assert "OLD_CADDY_IMAGE_ID" in capture
    on_exit = script[script.index("on_exit() {") : script.index("trap on_exit EXIT")]
    assert '"$MIGRATION_STATE" == applied' in on_exit
    assert "rollback_infrastructure" in on_exit


def test_exact_detached_asset_root_bootstraps_before_source_checkout() -> None:
    script = _text(DEPLOY)
    provision = _text(ROOT / "deploy" / "provision-release-host.sh")
    overlay = _text(ROOT / "deploy" / "compose.release.yml")
    for contract in (
        "EURITH_DEPLOY_ASSET_ROOT",
        "deploy_asset_root_not_detached",
        "deploy_asset_root_sha_mismatch",
        "deploy_asset_root_dirty",
        "deploy_asset_overlay_mismatch",
        "EURITH_RUNTIME_SOURCE_ROOT",
        "EURITH_RUNTIME_ASSET_ROOT",
    ):
        assert contract in script or contract in provision or contract in overlay
    assert 'git -C "$EURITH_DEPLOY_ASSET_ROOT" symbolic-ref -q HEAD' in script
    assert 'git -C "$EURITH_DEPLOY_ASSET_ROOT" status --porcelain' in script
    assert 'git -C "$EURITH_DEPLOY_ASSET_ROOT" rev-parse HEAD' in script
    assert '--deploy-asset-root "$EURITH_DEPLOY_ASSET_ROOT"' in script
    assert '--target-sha "$TARGET_SHA"' in script

    rehearsal = script.index("CURRENT_STAGE=rehearse_migration_compatibility")
    source_checkout = script.index("CURRENT_STAGE=checkout_target_source")
    production_migration = script.index("CURRENT_STAGE=apply_migration_once")
    assert rehearsal < source_checkout < production_migration
    assert 'EURITH_RUNTIME_SOURCE_ROOT="$EURITH_DEPLOY_ASSET_ROOT"' in script
    assert 'EURITH_RUNTIME_ASSET_ROOT="$EURITH_DEPLOY_ASSET_ROOT"' in script
    assert 'EURITH_RUNTIME_SOURCE_ROOT="$SOURCE_DIR"' in script
    assert 'EURITH_RUNTIME_ASSET_ROOT="$SOURCE_DIR"' in script
    assert "runtime_asset_bytes_mismatch" in script
    assert "deploy_asset_sha256" in script
    assert "runtime_asset_sha256" in script


def test_deploy_orders_irreversible_work_behind_all_preflight_gates() -> None:
    script = _text(DEPLOY)
    ordered = [
        "gate_checkout",
        "gate_pause",
        "snapshot_rehearsal_inputs",
        "create_paired_backup",
        "verify_isolated_restore",
        "validate_caddy",
        "run_caddy_integration",
        "gate_migrations",
        "build_target",
        "rehearse_migration_compatibility",
        "capture_prior_runtime",
        "apply_migration_once",
        "switch_api_and_caddy",
        "wait_for_readiness",
        "verify_switched_container_stability",
        "run_public_canaries",
        "review_runtime_logs",
        "cleanup_rehearsal_inputs",
        "write_mobile_gate",
    ]
    positions = [script.index(f"CURRENT_STAGE={name}") for name in ordered]
    assert positions == sorted(positions)


def test_deploy_never_persists_rendered_compose_or_restores_database_automatically() -> None:
    script = _text(DEPLOY)
    assert '"${compose[@]}" config >/dev/null' in script
    assert "docker compose config >" not in script
    assert "pg_restore" not in script
    assert "alembic downgrade" not in script
    assert "git push --force" not in script
    assert "git reset --hard" not in script
    assert "rollback_infrastructure" in script
    assert "schema_rollback_compatible" in script
    assert not (ROOT / "deploy" / "migration-safety-gate.py").exists()


def test_deploy_records_required_redacted_evidence_and_pins_caddy_digest() -> None:
    script = _text(DEPLOY)
    publisher = _text(PUBLISH_GATE)
    for field in (
        "old_backend_sha",
        "rollback_backend_sha",
        "new_backend_sha",
        "mobile_candidate_sha",
        "compose_sha256",
        "caddy_image",
        "caddy_digest",
        "alembic_heads",
        "alembic_path",
        "backup_manifest_sha256",
        "canary_result",
        "rollback_result",
        "backend_gate",
    ):
        assert field in script
    assert "RepoDigests" in script
    assert "sha256:" in script
    for contract in (
        "backup_restore_manifest_mismatch",
        "migration_approval_file",
        "migration_state=unknown",
        "wait_for_readiness",
        "deploy_completed_at",
        "--pull never",
        "CADDY_IMAGE_REF",
        "rollback_checkout_mismatch",
        "migration_path_sha256",
        "migration_approval_identity",
        "rehearsal_probe_sha256",
        "OLD_CADDY_IMAGE_ID",
        "rollback_image_overlay",
        "unexpected_switched_container_restart",
    ):
        assert contract in script
    for contract in ("evidence_sha256", "evidence.parent.parent", "_write_all", "os.fstat", "os.link", "os.fsync"):
        assert contract in publisher
    assert 'CADDY_IMAGE_REF="$CADDY_IMAGE@$EURITH_APPROVED_CADDY_DIGEST"' in script


def test_canary_is_bounded_fail_closed_and_does_not_create_release_rows() -> None:
    script = _text(CANARY)
    for contract in (
        "EURITH_PUBLIC_API_URL",
        "EURITH_CANARY_IDS_FILE",
        "missing_release_id",
        "withdrawn_release_id",
        "non_direct_release_id",
        "Cache-Control",
        "no-store",
        "X-Accel-Redirect",
        "Range: bytes=0-0",
        "existing_direct_apk=not_applicable",
        "available_percent",
        "20",
        "CADDY_CONTAINER",
        "API_CONTAINER",
    ):
        assert contract in script
    assert "--max-time" in script
    assert "--max-filesize" in script
    assert "POST /internal/app-releases/android/direct-apk" not in script
    assert "INSERT INTO app_releases" not in script
    assert "262144000" in script
    assert "restart_baseline" in script
    assert "--tail" in script
    assert "http-header-gate.py" in script
    assert "urllib.parse" in script
    assert "unrelated_public_path" not in script


def test_canary_allows_category_sentinel_only_after_zero_row_database_proof() -> None:
    script = _text(CANARY)
    assert 'withdrawn_release_id" == not_applicable' in script
    assert 'non_direct_release_id" == not_applicable' in script
    assert "withdrawn_release=not_applicable" in script
    assert "non_direct_release=not_applicable" in script
    assert "delivery_method='direct_apk' AND status='withdrawn'" in script
    assert "delivery_method <> 'direct_apk'" in script
    assert "category_not_empty" in script
    assert script.index("category_not_empty") < script.index("canary_result=passed")


def test_canary_proves_each_concrete_uuid_registry_semantics_before_http() -> None:
    script = _text(CANARY)
    assert "registry_uuid_count" in script
    assert "missing_release_id_present" in script
    assert "withdrawn_release_id_category_mismatch" in script
    assert "non_direct_release_id_category_mismatch" in script
    assert "id = :'release_id'::uuid" in script
    missing_proof = script.index("missing_release_id_present")
    missing_http = script.index("request missing GET")
    withdrawn_proof = script.index("withdrawn_release_id_category_mismatch")
    withdrawn_http = script.index("request withdrawn GET")
    non_direct_proof = script.index("non_direct_release_id_category_mismatch")
    non_direct_http = script.index("request non_direct GET")
    assert missing_proof < missing_http
    assert withdrawn_proof < withdrawn_http
    assert non_direct_proof < non_direct_http


def test_header_gate_rejects_duplicate_folded_malformed_and_injected_headers(tmp_path: Path) -> None:
    valid = tmp_path / "valid.headers"
    valid.write_bytes(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/2 200\r\nCache-Control: no-store\r\nETag: ok\r\n\r\n")
    accepted = subprocess.run([sys.executable, HEADER_GATE, "exact", valid, "Cache-Control", "no-store"], capture_output=True)
    assert accepted.returncode == 0
    adversarial = {
        "duplicate": b"HTTP/2 200\r\nCache-Control: no-store\r\ncache-control: public\r\n\r\n",
        "folded": b"HTTP/2 200\r\nCache-Control: no-store\r\n injected\r\n\r\n",
        "malformed": b"HTTP/2 200\r\nCache-Control no-store\r\n\r\n",
        "nul": b"HTTP/2 200\r\nCache-Control: no-store\x00public\r\n\r\n",
        "status-control": b"HTTP/2 200 ok\x01\r\nCache-Control: no-store\r\n\r\n",
        "oversized": b"HTTP/2 200\r\nX-Fill: " + b"a" * 70_000 + b"\r\n\r\n",
    }
    for name, payload in adversarial.items():
        candidate = tmp_path / f"{name}.headers"; candidate.write_bytes(payload)
        result = subprocess.run([sys.executable, HEADER_GATE, "exact", candidate, "Cache-Control", "no-store"], capture_output=True)
        assert result.returncode != 0, name


def test_header_gate_absent_mode_checks_every_response_block(tmp_path: Path) -> None:
    candidate = tmp_path / "redirect.headers"
    candidate.write_bytes(b"HTTP/1.1 302 Found\r\nX-Accel-Redirect: /secret\r\n\r\nHTTP/2 404\r\n\r\n")
    result = subprocess.run([sys.executable, HEADER_GATE, "absent", candidate, "X-Accel-Redirect"], capture_output=True)
    assert result.returncode != 0


def test_mobile_gate_publisher_writes_all_bytes_and_never_links_partial_gate(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("publish_mobile_gate", PUBLISH_GATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    evidence_dir = tmp_path / "evidence" / "generation"; evidence_dir.mkdir(parents=True)
    evidence = evidence_dir / "deploy.env"; evidence.write_text("deployment_result=passed\n", encoding="ascii")
    gate = tmp_path / "evidence" / "gate.env"
    monkeypatch.setattr(module, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(module.os, "fsync", lambda _descriptor: None)
    real_write = module.os.write
    monkeypatch.setattr(module.os, "write", lambda fd, data: real_write(fd, data[:3]))
    module.publish(evidence, gate, "a" * 40, "b" * 40, "2026-09-11T00:00:00Z")
    payload = gate.read_bytes()
    assert payload.endswith(b"write_mobile_gate_exit=0\n")
    gate.unlink()
    monkeypatch.setattr(module.os, "write", lambda _fd, _data: 0)
    try:
        module.publish(evidence, gate, "a" * 40, "b" * 40, "2026-09-11T00:00:00Z")
    except OSError:
        pass
    else:
        raise AssertionError("zero-length write must fail")
    assert not gate.exists()


def test_migration_path_manifest_hashes_exact_path_file_bytes(tmp_path: Path) -> None:
    versions = tmp_path / "versions"; versions.mkdir()
    first = versions / "001_first.py"; second = versions / "002_second.py"
    first.write_text("revision = '001'\ndown_revision = None\n", encoding="utf-8")
    second.write_text("revision = '002'\ndown_revision = '001'\n", encoding="utf-8")
    initial = subprocess.run([sys.executable, MIGRATION_MANIFEST, versions, "001"], capture_output=True, text=True)
    assert initial.returncode == 0
    assert "old_head=001\ntarget_head=002\nmigration_path=001->002\n" in initial.stdout
    assert "migration_file=002:002_second.py:" in initial.stdout
    second.write_text("revision = '002'\ndown_revision = '001'\n# reviewed byte change\n", encoding="utf-8")
    changed = subprocess.run([sys.executable, MIGRATION_MANIFEST, versions, "001"], capture_output=True, text=True)
    assert changed.returncode == 0
    initial_hash = initial.stdout.split("migration_path_sha256=", 1)[1].splitlines()[0]
    changed_hash = changed.stdout.split("migration_path_sha256=", 1)[1].splitlines()[0]
    assert initial_hash != changed_hash
    assert subprocess.run([sys.executable, MIGRATION_MANIFEST, versions, "missing"], capture_output=True).returncode != 0


def test_migration_approval_is_exact_root_owned_mode_0400_and_identity_bound(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("migration_approval", MIGRATION_APPROVAL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    approval = tmp_path / "approval.env"
    old_sha, target_sha, rollback_sha, path_hash = "a" * 40, "b" * 40, "d" * 40, "c" * 64
    lines = [
        f"old_backend_sha={old_sha}", f"target_backend_sha={target_sha}",
        f"rollback_backend_sha={rollback_sha}",
        "old_alembic_head=001", "target_alembic_head=002",
        f"migration_path_sha256={path_hash}", "rollback_compatible=true",
        "approval_identity=release-reviewer@example.invalid",
    ]
    approval.write_bytes(("\n".join(lines) + "\n").encode("ascii"))
    class Metadata:
        st_mode = 0o100400
        st_uid = 0
        st_gid = 0
    raw = approval.read_bytes()
    assert module.validate_payload(Metadata(), raw, old_sha, target_sha, rollback_sha, "001", "002", path_hash)[1] == "release-reviewer@example.invalid"
    Metadata.st_mode = 0o100600
    try:
        module.validate_payload(Metadata(), raw, old_sha, target_sha, rollback_sha, "001", "002", path_hash)
    except ValueError:
        pass
    else:
        raise AssertionError("mode 0600 must be rejected")
    Metadata.st_mode = 0o120400
    try:
        module.validate_payload(Metadata(), raw, old_sha, target_sha, rollback_sha, "001", "002", path_hash)
    except ValueError:
        pass
    else:
        raise AssertionError("symlink approval must be rejected")
    Metadata.st_mode = 0o100400
    approval.write_bytes(("\n".join(lines[:-1] + ["approval_identity=bad identity"]) + "\n").encode("ascii"))
    try:
        module.validate_payload(Metadata(), approval.read_bytes(), old_sha, target_sha, rollback_sha, "001", "002", path_hash)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid approval identity must be rejected")
    mismatch = ("\n".join(lines) + "\n").replace("target_alembic_head=002", "target_alembic_head=evil")
    approval.write_bytes(mismatch.encode("ascii"))
    try:
        module.validate_payload(Metadata(), approval.read_bytes(), old_sha, target_sha, rollback_sha, "001", "002", path_hash)
    except ValueError:
        pass
    else:
        raise AssertionError("approval mismatch must be rejected")


def test_migration_rehearsal_is_mandatory_and_fail_closed_before_production_mutation() -> None:
    script = _text(DEPLOY)
    assert "migration-safety-gate.py" not in script
    assert "migration_path_sha256" in script
    assert "rollback_compatible=true" in _text(MIGRATION_APPROVAL)
    assert "approval_identity" in script
    assert 'CURRENT_STAGE=rehearse_migration_compatibility; rehearse_migration_compatibility' in script
    assert '|| die target_migration_rehearsal_failed' in script
    assert '|| die target_schema_rehearsal_failed' in script
    assert '|| die rollback_candidate_compatibility_rehearsal_failed' in script
    probe_command = '--workdir /app -e PYTHONPATH=/app api python /tmp/eurith-rehearse-release-db.py "$EXPECTED_ALEMBIC_HEAD"'
    assert script.count(probe_command) == 2
    assert script.index("CURRENT_STAGE=rehearse_migration_compatibility") < script.index("CURRENT_STAGE=apply_migration_once")
    assert script.index("SCHEMA_ROLLBACK_COMPATIBLE=1") < script.index("CURRENT_STAGE=apply_migration_once")
    probe = _text(REHEARSAL_PROBE)
    for contract in (
        "health_check",
        "alembic_version",
        "Base.metadata.sorted_tables",
        "missing_table",
        "missing_column",
        "unsafe_training_block_provenance",
        "invalid_release_registry_row",
        "phase_snapshot_trusted = false",
        "FROM app_releases",
    ):
        assert contract in probe


def test_rehearsal_secret_cleanup_precedes_durable_mobile_gate_and_failure_fsync() -> None:
    script = _text(DEPLOY)
    cleanup_stage = script.index("CURRENT_STAGE=cleanup_rehearsal_inputs")
    mobile_stage = script.index("CURRENT_STAGE=write_mobile_gate")
    assert cleanup_stage < mobile_stage
    assert "evidence rehearsal_input_cleanup passed" in script[cleanup_stage:mobile_stage]
    on_exit = script[script.index("on_exit() {"):script.index("trap on_exit EXIT")]
    assert on_exit.index("evidence rehearsal_input_cleanup failed") < on_exit.index("os.fsync(handle.fileno())")
    assert "cleanup_rehearsal_inputs" not in script[mobile_stage:]


def test_rehearsal_database_url_is_snapshotted_once_and_yaml_paths_are_serialized_safely() -> None:
    spec = importlib.util.spec_from_file_location("rehearsal_inputs", REHEARSAL_INPUTS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    local = b"DATABASE_URL=postgresql+asyncpg://restore@127.0.0.1/eurith_restore_release_1\n"
    assert module.validate_database_url(local) == local
    for unsafe in (
        b"DATABASE_URL=postgresql+asyncpg://prod@db.internal/eurith\n",
        b"DATABASE_URL=postgresql+asyncpg://restore@127.0.0.1/production\n",
        local + b"DATABASE_URL=postgresql+asyncpg://prod@db.internal/eurith\n",
    ):
        try:
            module.validate_database_url(unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe rehearsal database target must be rejected")
    injected = '/tmp/probe": privileged: true #'
    overlay = module.overlay_bytes("/tmp/private/restore-db.env", injected).decode("ascii")
    assert '\\"' in overlay
    try:
        module.overlay_bytes("/tmp/private/restore-db.env", "/tmp/probe\n    privileged: true")
    except ValueError:
        pass
    else:
        raise AssertionError("control characters in overlay paths must be rejected")
    helper = _text(REHEARSAL_INPUTS)
    for contract in ("O_NOFOLLOW", "dir_fd", "database_url_parent_untrusted", "os.fstat", "_write_all", "os.fsync", "tempfile.mkdtemp", "json.dumps"):
        assert contract in helper
    script = _text(DEPLOY)
    assert 'require_safe_absolute_path "$EURITH_RESTORE_DB_URL_FILE" secret "$SOURCE_DIR"' in script
    assert '"$REHEARSAL_DB_URL_SNAPSHOT" "$EURITH_RESTORE_VOLUME_ROOT"' in script
    assert '"$EURITH_BACKUP_ROOT/$BACKUP_GENERATION" "$EURITH_RESTORE_DB_URL_FILE"' not in script
    assert script.index("CURRENT_STAGE=snapshot_rehearsal_inputs") < script.index("CURRENT_STAGE=create_paired_backup")


def test_rollback_restores_both_prior_caddy_topologies() -> None:
    script = _text(DEPLOY)
    assert 'if [[ "$PRIOR_CADDY_PRESENT" == 1 ]]' in script
    assert 'rollback_compose+=(-f "$rollback_image_overlay")' in script
    assert '"$rollback_caddy_image" == "$OLD_CADDY_IMAGE_ID"' in script
    assert 'if [[ "$PRIOR_CADDY_PRESENT" == 0 ]]' in script
    assert '"${compose[@]}" rm -f caddy' in script
    assert 'rollback_compose=(docker compose --env-file "$DEPLOY_ENV" -f "$EURITH_BASE_COMPOSE" -f "$RELEASE_OVERLAY")' in script


@pytest.mark.parametrize("case", [
    "caddy_crash_before", "caddy_crash_after", "caddy_restart_after",
    "caddy_restart_invalid", "caddy_swap_after", "caddy_image_after", "caddy_missing_after",
])
def test_rollback_caddy_must_remain_running_same_image_container_and_restart_count(tmp_path: Path, case: str) -> None:
    evidence = _run_rollback_harness(tmp_path, case, True)
    assert "rollback_result=failed" in evidence
    assert "rollback_result=passed" not in evidence


def test_rollback_without_prior_caddy_rejects_container_appearing_during_readiness(tmp_path: Path) -> None:
    evidence = _run_rollback_harness(tmp_path, "caddy_unexpected", False)
    assert "rollback_result=failed" in evidence
    assert "rollback_result=passed" not in evidence


def test_rollback_pass_requires_exact_candidate_api_image_running_stable_and_ready() -> None:
    script = _text(DEPLOY)
    rollback = script[script.index("rollback_infrastructure()") : script.index("switch_api_and_caddy()")]
    assert 'config --images api' in rollback
    assert "docker image inspect --format '{{.Id}}'" in rollback
    assert "--format '{{.Image}}'" in rollback
    assert "--format '{{.State.Status}}'" in rollback
    assert "--format '{{.RestartCount}}'" in rollback
    assert "rollback_api_readiness" in rollback
    assert "rollback_api_runtime" in rollback
    assert "rollback_api_image_verified" in rollback
    assert rollback.count("result=passed") == 1
    assert '"$rollback_api_image" == "$rollback_built_api_image"' in rollback
    assert '"$rollback_api_status" == running' in rollback
    assert '"$rollback_api_restarts" == 0' in rollback
    assert "for attempt in $(seq 1 30)" in rollback
    assert '"$PUBLIC_API_BASE/health"' in rollback
    assert "rollback_api_verified=1" in rollback
    assert "rollback_caddy_verified=1" in rollback
    assert '[[ "$rollback_api_verified" == 1 && "$rollback_caddy_verified" == 1 ]] && result=passed' in rollback
    assert 'up -d --no-deps api >/dev/null 2>&1 && result=passed' not in rollback


@pytest.mark.parametrize(
    ("case", "prior_caddy"),
    [
        ("up_failure", True),
        ("wrong_image", False),
        ("not_running", True),
        ("restart_before", False),
        ("restart_after", True),
        ("readiness_timeout", False),
        ("container_swap", True),
    ],
)
def test_rollback_runtime_failures_never_emit_passed(
    tmp_path: Path, case: str, prior_caddy: bool
) -> None:
    evidence = _run_rollback_harness(tmp_path, case, prior_caddy)
    assert "rollback_result=failed" in evidence
    assert "rollback_result=passed" not in evidence


@pytest.mark.parametrize("prior_caddy", [False, True])
def test_rollback_runtime_success_is_proven_for_both_caddy_topologies(
    tmp_path: Path, prior_caddy: bool
) -> None:
    evidence = _run_rollback_harness(tmp_path, "success", prior_caddy)
    assert evidence.count("rollback_result=passed") == 1
    assert "rollback_api_image=passed" in evidence
    assert "rollback_api_runtime=passed" in evidence
    assert "rollback_api_readiness=passed" in evidence
    expected_caddy = "rollback_caddy_image=passed" if prior_caddy else "rollback_caddy_image=not_applicable"
    assert expected_caddy in evidence


def test_nginx_artifact_and_instructions_are_removed_together() -> None:
    assert not (ROOT / "deploy" / "nginx" / "releases.conf").exists()
    combined = _text(README).lower() + _text(CHECKLIST).lower()
    assert "deploy/nginx/releases.conf" not in combined
    assert "nginx -t" not in combined
    assert "caddy:2.11.4" in combined
    assert "successful push is not deployment" in combined
    assert "successful backend deployment is not apk publication" in combined


def test_runbooks_cover_first_use_rotation_recovery_and_mobile_gate() -> None:
    combined = _text(README) + _text(CHECKLIST)
    for contract in (
        "provision-release-host.sh",
        "backup-release-state.sh",
        "verify-release-restore.sh",
        "canary-release-delivery.sh",
        "withdraw",
        "backend_gate=passed",
        "free space",
        "framing",
        "rotation",
        "EURITH_DEPLOY_ASSET_ROOT",
        "detached",
        "EURITH_RUNTIME_ASSET_ROOT",
    ):
        assert contract.lower() in combined.lower()


def test_dirty_checkout_stops_before_any_mutating_command(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    _wrapper(bin_dir, "git", 'printf "git %s\\n" "$*" >>"$FAKE_CALLS"; [[ "$1 $2" == "status --porcelain" ]] && printf " M dirty\\n"')
    _wrapper(bin_dir, "stat", 'if [[ "$*" == *"restore-db"* ]]; then printf "600:0\\n"; else printf "640:0\\n"; fi')
    for command in ("docker", "curl", "sha256sum", "awk", "sed", "grep", "findmnt", "head", "systemctl"):
        _wrapper(bin_dir, command, f'printf "{command} %s\\n" "$*" >>"$FAKE_CALLS"; exit 99')
    _wrapper(bin_dir, "python3", f'exec "{_shell(Path(sys.executable))}" "$@"')
    source = tmp_path / "checkout"; source.mkdir(); (source / ".git").mkdir()
    base = tmp_path / "compose.yml"; base.write_text("services: {}\n")
    overlay = tmp_path / "overlay.yml"; overlay.write_text("services: {}\n")
    deploy_env = tmp_path / "deploy.env"; deploy_env.write_text("RELEASE_SHARED_GID=1\n")
    ids = tmp_path / "ids.env"; ids.write_text("x=y\n")
    restore_db = tmp_path / "restore-db"; restore_db.write_text("unused\n")
    migration_approval = tmp_path / "migration-approval.env"; migration_approval.write_text("unused\n")
    restore_root = tmp_path / "restore"; restore_root.mkdir()
    env = os.environ.copy(); env.update({
        "PATH": _shell(bin_dir) + ":/usr/bin:/bin", "FAKE_CALLS": _shell(calls),
        "SOURCE_DIR": _shell(source), "APP_DIR": _shell(tmp_path / "app"),
        "EURITH_DEPLOY_ASSET_ROOT": _shell(ROOT),
        "EURITH_BASE_COMPOSE": _shell(base), "RELEASE_OVERLAY": _shell(ROOT / "deploy" / "compose.release.yml"),
        "DEPLOY_ENV": _shell(deploy_env), "EURITH_PUBLIC_API_URL": "https://example.invalid",
        "EURITH_CANARY_IDS_FILE": _shell(ids), "EURITH_BACKUP_ROOT": _shell(tmp_path / "backups"),
        "EURITH_RESTORE_DB_URL_FILE": _shell(restore_db), "EURITH_RESTORE_VOLUME_ROOT": _shell(restore_root),
        "EURITH_MIGRATION_APPROVAL_FILE": _shell(migration_approval),
        "EURITH_EVIDENCE_ROOT": _shell(tmp_path / "evidence"), "MOBILE_GATE_FILE": _shell(tmp_path / "gate.env"),
        "EURITH_APPROVED_CADDY_DIGEST": "sha256:" + "c" * 64,
        "EURITH_ROLLBACK_SHA": "d" * 40,
    })
    result = subprocess.run([BASH, _shell(DEPLOY), "a" * 40, "b" * 40], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "error=checkout_dirty" in result.stderr
    assert calls.read_text().splitlines() == ["git status --porcelain"]


def test_failed_negative_auth_canary_stops_after_read_only_restart_baseline(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"; bin_dir.mkdir(); calls = tmp_path / "calls.log"
    ids = tmp_path / "ids.env"
    ids.write_bytes((
        "missing_release_id=11111111-1111-4111-8111-111111111111\n"
        "withdrawn_release_id=22222222-2222-4222-8222-222222222222\n"
        "non_direct_release_id=33333333-3333-4333-8333-333333333333\n"
    ).encode())
    driver = tmp_path / "curl_driver.py"
    driver.write_text(
        "import pathlib,sys\nargs=sys.argv[1:]\n"
        "h=pathlib.Path(args[args.index('--dump-header')+1]); b=pathlib.Path(args[args.index('--output')+1]); u=args[-1]\n"
        "h.write_bytes(b'HTTP/2 200\\r\\nCache-Control: no-store\\r\\n\\r\\n'); b.write_text('{\"database\":\"connected\"}')\n"
        "print('200' if ('health' in u or 'latest' in u or 'lanes/android' in u) else '404',end='')\n",
        encoding="utf-8",
    )
    _wrapper(bin_dir, "curl", f'exec "{_shell(Path(sys.executable))}" "{_shell(driver)}" "$@"')
    _wrapper(bin_dir, "python3", f'exec "{_shell(Path(sys.executable))}" "$@"')
    _wrapper(bin_dir, "stat", 'if [[ "$*" == *"%a:%u:%g"* ]]; then printf "400:0:0\\n"; else exec /usr/bin/stat "$@"; fi')
    _wrapper(bin_dir, "docker", '''printf "docker %s\\n" "$*" >>"$FAKE_CALLS"
case "$*" in
  *" ps --status running --services") printf "api\\ncaddy\\n" ;;
  *" ps -q api") printf "api-id\\n" ;;
  *" ps -q caddy") printf "caddy-id\\n" ;;
  "inspect api-id --format {{.RestartCount}}"|"inspect caddy-id --format {{.RestartCount}}") printf "2\\n" ;;
  *) exit 99 ;;
esac''')
    env = os.environ.copy(); env.update({"PATH": _shell(bin_dir) + ":/usr/bin:/bin", "FAKE_CALLS": _shell(calls), "EURITH_PUBLIC_API_URL": "https://example.invalid", "EURITH_CANARY_IDS_FILE": _shell(ids)})
    result = subprocess.run([BASH, _shell(CANARY)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "error=canary_http_status_failed" in result.stderr
    docker_calls = calls.read_text()
    assert " inspect " not in docker_calls or "RestartCount" in docker_calls
    assert " logs " not in docker_calls
    assert " exec " not in docker_calls
    assert " up " not in docker_calls


@pytest.mark.parametrize("non_direct_case", ["nonexistent", "wrong_existing_category"])
def test_canary_rejects_non_direct_uuid_without_matching_registry_row(
    tmp_path: Path, non_direct_case: str
) -> None:
    bin_dir = tmp_path / "bin"; bin_dir.mkdir(); calls = tmp_path / "calls.log"
    ids = tmp_path / "ids.env"
    ids.write_bytes((
        "missing_release_id=11111111-1111-4111-8111-111111111111\n"
        "withdrawn_release_id=22222222-2222-4222-8222-222222222222\n"
        "non_direct_release_id=33333333-3333-4333-8333-333333333333\n"
    ).encode("ascii"))
    driver = tmp_path / "curl_driver.py"
    driver.write_text(
        "import pathlib,sys\nargs=sys.argv[1:]\n"
        "h=pathlib.Path(args[args.index('--dump-header')+1]); b=pathlib.Path(args[args.index('--output')+1]); u=args[-1]\n"
        "status=200\n"
        "if 'lanes/android' in u or '/mandatory' in u or '/webhooks/' in u: status=401\n"
        "elif '11111111-' in u or '33333333-' in u: status=404\n"
        "elif '22222222-' in u: status=410\n"
        "headers=b'HTTP/2 '+str(status).encode()+b'\\r\\n'+(b'Cache-Control: no-store\\r\\n' if 'latest?' in u else b'')+b'\\r\\n'\n"
        "h.write_bytes(headers); b.write_text('{\"database\":\"connected\"}' if '/health' in u else '{}')\n"
        "print(status,end='')\n",
        encoding="utf-8",
    )
    _wrapper(bin_dir, "curl", f'exec "{_shell(Path(sys.executable))}" "{_shell(driver)}" "$@"')
    _wrapper(bin_dir, "python3", f'exec "{_shell(Path(sys.executable))}" "$@"')
    _wrapper(bin_dir, "stat", 'if [[ "$*" == *"%a:%u:%g"* ]]; then printf "400:0:0\\n"; else exec /usr/bin/stat "$@"; fi')
    _wrapper(bin_dir, "docker", '''printf "docker %s\n" "$*" >>"$FAKE_CALLS"
case "$*" in
  *" ps --status running --services") printf "api\ncaddy\n" ;;
  *" ps -q api") printf "api-id\n" ;;
  *" ps -q caddy") printf "caddy-id\n" ;;
  "inspect api-id --format {{.RestartCount}}"|"inspect caddy-id --format {{.RestartCount}}") printf "2\n" ;;
  *"id = :'release_id'::uuid AND delivery_method='direct_apk' AND status='withdrawn'"*) printf "1\n" ;;
  *"id = :'release_id'::uuid AND delivery_method <> 'direct_apk'"*) printf "0\n" ;;
  *"id = :'release_id'::uuid"*) printf "0\n" ;;
  *) exit 99 ;;
esac''')
    env = os.environ.copy(); env.update({
        "PATH": _shell(bin_dir) + ":/usr/bin:/bin", "FAKE_CALLS": _shell(calls),
        "EURITH_PUBLIC_API_URL": "https://example.invalid", "EURITH_CANARY_IDS_FILE": _shell(ids),
        "NON_DIRECT_CASE": non_direct_case,
    })
    result = subprocess.run(
        [BASH, _shell(CANARY)], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0
    assert "error=non_direct_release_id_category_mismatch" in result.stderr
    assert " logs " not in calls.read_text()
