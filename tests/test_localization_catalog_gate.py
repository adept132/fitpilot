import pytest

from scripts import localization_catalog_gate as gate


EXPECTED_IDS = set(range(76, 270))


def _catalog_ids():
    return {exercise_id: object() for exercise_id in EXPECTED_IDS}


def _allow_expected_catalog_and_database(monkeypatch):
    monkeypatch.setattr(gate.backfill, "load_translations", lambda path: _catalog_ids())

    async def expected_database_ids():
        return set(EXPECTED_IDS)

    monkeypatch.setattr(gate, "_default_exercise_ids", expected_database_ids)


@pytest.mark.asyncio
async def test_gate_applies_then_checks_catalog(monkeypatch):
    calls = []

    async def fake_run(apply, translations_path):
        calls.append(apply)
        return 0

    _allow_expected_catalog_and_database(monkeypatch)
    monkeypatch.setattr(gate.backfill, "_run", fake_run)

    assert await gate.run_gate() == 0
    assert calls == [True, False]


@pytest.mark.asyncio
async def test_gate_stops_before_check_when_apply_fails(monkeypatch):
    calls = []

    async def fake_run(apply, translations_path):
        calls.append(apply)
        return 1

    _allow_expected_catalog_and_database(monkeypatch)
    monkeypatch.setattr(gate.backfill, "_run", fake_run)

    assert await gate.run_gate() == 1
    assert calls == [True]


@pytest.mark.asyncio
async def test_gate_stops_before_apply_when_catalog_is_missing_an_expected_id(monkeypatch):
    catalog = _catalog_ids()
    catalog.pop(76)
    monkeypatch.setattr(gate.backfill, "load_translations", lambda path: catalog)

    async def unexpected_database_access():
        raise AssertionError("catalog must fail before checking database IDs")

    monkeypatch.setattr(gate, "_default_exercise_ids", unexpected_database_access)

    assert await gate.run_gate() == 1


@pytest.mark.asyncio
async def test_gate_stops_before_apply_when_catalog_has_an_extra_id(monkeypatch):
    catalog = _catalog_ids()
    catalog[270] = object()
    monkeypatch.setattr(gate.backfill, "load_translations", lambda path: catalog)

    async def unexpected_database_access():
        raise AssertionError("catalog must fail before checking database IDs")

    monkeypatch.setattr(gate, "_default_exercise_ids", unexpected_database_access)

    assert await gate.run_gate() == 1


@pytest.mark.asyncio
async def test_gate_stops_before_apply_when_database_is_missing_an_expected_id(monkeypatch):
    _allow_expected_catalog_and_database(monkeypatch)

    async def missing_database_id():
        return set(EXPECTED_IDS) - {76}

    async def unexpected_apply(apply, translations_path):
        raise AssertionError("database IDs must be checked before applying")

    monkeypatch.setattr(gate, "_default_exercise_ids", missing_database_id)
    monkeypatch.setattr(gate.backfill, "_run", unexpected_apply)

    assert await gate.run_gate() == 1


@pytest.mark.asyncio
async def test_gate_stops_before_apply_when_database_has_an_extra_id(monkeypatch):
    _allow_expected_catalog_and_database(monkeypatch)

    async def extra_database_id():
        return set(EXPECTED_IDS) | {270}

    async def unexpected_apply(apply, translations_path):
        raise AssertionError("database IDs must be checked before applying")

    monkeypatch.setattr(gate, "_default_exercise_ids", extra_database_id)
    monkeypatch.setattr(gate.backfill, "_run", unexpected_apply)

    assert await gate.run_gate() == 1


def test_deploy_requires_a_full_commit_sha_and_validates_backup_before_checkout():
    deploy_script = (
        gate.REPO_ROOT / "deploy" / "deploy.sh"
    ).read_text(encoding="utf-8")

    assert '[[ "$TARGET_SHA" =~ ^[0-9a-fA-F]{40}$ ]]' in deploy_script
    assert 'git rev-parse --verify "${TARGET_SHA}^{commit}"' in deploy_script
    assert '[[ "$NEW_COMMIT" != "$TARGET_SHA" ]]' in deploy_script
    assert "pg_restore --list \"$BACKUP_FILE\"" in deploy_script
    assert 'BACKUP_SHA256="$(sha256sum \"$BACKUP_FILE\"' in deploy_script
    assert deploy_script.index('git checkout --detach "$NEW_COMMIT"') > deploy_script.index(
        'pg_restore --list "$BACKUP_FILE"'
    )


def test_deploy_rolls_back_api_start_and_health_failures_with_backup_evidence():
    deploy_script = (
        gate.REPO_ROOT / "deploy" / "deploy.sh"
    ).read_text(encoding="utf-8")

    assert "rollback_api" in deploy_script
    assert 'if ! docker compose up -d --no-deps api; then' in deploy_script
    assert 'rollback_api "API failed to start"' in deploy_script
    assert 'rollback_api "Health check failed"' in deploy_script
    assert 'echo "Backup: $BACKUP_FILE" >&2' in deploy_script
    assert 'echo "Backup SHA-256: $BACKUP_SHA256" >&2' in deploy_script
