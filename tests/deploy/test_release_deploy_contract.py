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
    _wrapper(bin_dir, "curl", '''if [[ "$ROLLBACK_CASE" == readiness_timeout ]]; then printf 503; else printf 200; fi''')
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
    *" ps -q caddy") [[ "$PRIOR_CADDY" == 1 && "$ROLLBACK_CASE" != up_failure ]] && printf "caddy-id\\n" ;;
    *) exit 96 ;;
  esac
elif [[ "$1 $2" == "image inspect" ]]; then
  printf "%s\\n" "{built_image}"
elif [[ "$1" == inspect ]]; then
  container="$2"; format="$4"
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
    for command in ("docker", "curl", "sha256sum", "awk", "sed", "grep", "findmnt", "head"):
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
