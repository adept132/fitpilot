from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "deploy.sh"
CANARY = ROOT / "deploy" / "canary-release-delivery.sh"
HEADER_GATE = ROOT / "deploy" / "http-header-gate.py"
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


def test_deploy_requires_two_exact_shas_and_remote_containment() -> None:
    script = _text(DEPLOY)
    assert "[[ $# == 2 ]]" in script
    assert 'require_full_sha "$TARGET_SHA"' in script
    assert 'require_full_sha "$MOBILE_CANDIDATE_SHA"' in script
    assert "git status --porcelain" in script
    assert "git merge-base --is-ancestor" in script
    assert "APPROVED_REMOTE_REF" in script
    assert "deploy_runner_sha_mismatch" in script


def test_deploy_orders_irreversible_work_behind_all_preflight_gates() -> None:
    script = _text(DEPLOY)
    ordered = [
        "gate_checkout",
        "gate_pause",
        "create_paired_backup",
        "verify_isolated_restore",
        "validate_caddy",
        "run_caddy_integration",
        "gate_migrations",
        "build_target",
        "apply_migration_once",
        "switch_api_and_caddy",
        "wait_for_readiness",
        "run_public_canaries",
        "review_runtime_logs",
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
    assert "ast.walk(upgrade)" in script


def test_deploy_records_required_redacted_evidence_and_pins_caddy_digest() -> None:
    script = _text(DEPLOY)
    for field in (
        "old_backend_sha",
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
        "evidence_sha256",
        "os.link",
        "fsync",
        "--pull never",
        "CADDY_IMAGE_REF",
        "rollback_checkout_mismatch",
    ):
        assert contract in script
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
    ):
        assert contract.lower() in combined.lower()


def test_dirty_checkout_stops_before_any_mutating_command(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    _wrapper(bin_dir, "git", 'printf "git %s\\n" "$*" >>"$FAKE_CALLS"; [[ "$1 $2" == "status --porcelain" ]] && printf " M dirty\\n"')
    _wrapper(bin_dir, "stat", 'printf "640:0\\n"')
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
        "EURITH_BASE_COMPOSE": _shell(base), "RELEASE_OVERLAY": _shell(overlay),
        "DEPLOY_ENV": _shell(deploy_env), "EURITH_PUBLIC_API_URL": "https://example.invalid",
        "EURITH_CANARY_IDS_FILE": _shell(ids), "EURITH_BACKUP_ROOT": _shell(tmp_path / "backups"),
        "EURITH_RESTORE_DB_URL_FILE": _shell(restore_db), "EURITH_RESTORE_VOLUME_ROOT": _shell(restore_root),
        "EURITH_MIGRATION_APPROVAL_FILE": _shell(migration_approval),
        "EURITH_EVIDENCE_ROOT": _shell(tmp_path / "evidence"), "MOBILE_GATE_FILE": _shell(tmp_path / "gate.env"),
        "EURITH_APPROVED_CADDY_DIGEST": "sha256:" + "c" * 64,
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
