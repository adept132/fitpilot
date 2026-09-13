from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "deploy" / "guard-compose-database-url.py"


def _guard():
    spec = importlib.util.spec_from_file_location("compose_database_guard", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(url: str | None) -> bytes:
    environment = {} if url is None else {"DATABASE_URL": url}
    return json.dumps({"services": {"api": {"environment": environment}}}).encode()


def test_production_guard_accepts_only_effective_protected_database_url() -> None:
    guard = _guard()
    production = "postgresql+asyncpg://prod:secret@postgres:5432/fitpilot"
    restore = "postgresql+asyncpg://restore:secret@127.0.0.1/eurith_restore_release_1"
    expected = f"OTHER_TOKEN=abc\nDATABASE_URL={production}\n".encode()
    guard.verify(_config(production), expected)
    for actual in (restore, "", None):
        with pytest.raises(ValueError):
            guard.verify(_config(actual), expected)


def test_rehearsal_guard_rejects_production_and_duplicate_expected_key() -> None:
    guard = _guard()
    production = "postgresql+asyncpg://prod:secret@postgres:5432/fitpilot"
    restore = "postgresql+asyncpg://restore:secret@127.0.0.1/eurith_restore_release_1"
    guard.verify(_config(restore), f"DATABASE_URL={restore}\n".encode())
    with pytest.raises(ValueError):
        guard.verify(_config(production), f"DATABASE_URL={restore}\n".encode())
    with pytest.raises(ValueError):
        guard.verify(_config(restore), f"DATABASE_URL={restore}\nDATABASE_URL={production}\n".encode())


def test_guard_error_never_exposes_secret_url() -> None:
    guard = _guard()
    secret = "postgresql+asyncpg://prod:never-print-me@postgres:5432/fitpilot"
    with pytest.raises(ValueError) as failure:
        guard.verify(_config("wrong"), f"DATABASE_URL={secret}\n".encode())
    assert "never-print-me" not in str(failure.value)


def test_guard_fails_closed_on_non_ascii_effective_url() -> None:
    guard = _guard()
    with pytest.raises(ValueError):
        guard.verify(_config("postgresql+asyncpg://испорчен"), b"DATABASE_URL=postgresql+asyncpg://prod@postgres/db\n")


def test_deploy_checks_production_and_rehearsal_effective_urls_before_migration() -> None:
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    production_gate = script[script.index("validate_caddy() {"):script.index("run_caddy_integration() {")]
    rehearsal_gate = script[script.index("rehearse_migration_compatibility() {"):script.index("apply_migration_once() {")]
    assert '"${compose[@]}" config --format json | python3 "$COMPOSE_DATABASE_GUARD" /etc/eurith/api-release.env' in production_gate
    assert '"${target_rehearsal_compose[@]}" config --format json | python3 "$COMPOSE_DATABASE_GUARD" "$REHEARSAL_DB_URL_SNAPSHOT"' in rehearsal_gate
    assert rehearsal_gate.index('"$REHEARSAL_DB_URL_SNAPSHOT"') < rehearsal_gate.index("api alembic upgrade head")


def test_release_compose_resets_base_database_environment_override() -> None:
    overlay = (ROOT / "deploy" / "compose.release.yml").read_text(encoding="utf-8")
    api = overlay.split("  api:\n", 1)[1].split("\n  caddy:", 1)[0]
    assert "    environment:\n      DATABASE_URL: !reset null\n" in api
