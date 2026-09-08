import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from api.services.models import AppRelease, AppReleaseLane


def test_migration_graph_has_exactly_one_head():
    """Catches an integration merge that leaves deployable migrations branched."""
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))

    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1, f"expected one migration head, found {heads}"


def test_release_tables_have_required_constraints():
    """Catches accidental removal of the database protections release writes rely on."""
    assert AppReleaseLane.__table__.primary_key.columns.keys() == ["platform", "channel"]
    names = {item.name for item in AppRelease.__table__.constraints if item.name}
    assert "uq_app_releases_idempotency_key" in names
    assert "ck_app_releases_delivery_payload" in names


def test_release_lane_metadata_matches_the_lane_ledger_contract():
    """Catches a release lane that cannot safely record its expected CI target."""
    columns = AppReleaseLane.__table__.c

    assert list(columns.keys()) == [
        "platform",
        "channel",
        "expected_source_commit",
        "expected_ci_run_id",
        "updated_at",
    ]
    assert isinstance(columns.platform.type, String)
    assert columns.platform.type.length == 16
    assert isinstance(columns.channel.type, String)
    assert columns.channel.type.length == 32
    assert isinstance(columns.expected_source_commit.type, String)
    assert columns.expected_source_commit.type.length == 40
    assert isinstance(columns.expected_ci_run_id.type, String)
    assert columns.expected_ci_run_id.type.length == 128
    # A webhook moves the SHA first; a protected CI run binds later.
    assert columns.expected_ci_run_id.nullable is True
    assert isinstance(columns.updated_at.type, DateTime)
    assert columns.updated_at.type.timezone is True
    assert columns.updated_at.server_default is not None


def test_release_metadata_has_the_immutable_publish_record_contract():
    """Catches omission or relaxation of release metadata needed by publish, update, and cleanup flows."""
    columns = AppRelease.__table__.c

    assert list(columns.keys()) == [
        "id",
        "platform",
        "channel",
        "delivery_method",
        "version_code",
        "version_name",
        "runtime_version",
        "fingerprint",
        "release_notes",
        "status",
        "is_mandatory",
        "min_supported_version_code",
        "artifact_storage_key",
        "artifact_sha256",
        "artifact_size_bytes",
        "source_commit",
        "ci_run_id",
        "idempotency_key",
        "eas_build_id",
        "eas_update_id",
        "eas_update_group_id",
        "published_at",
        "withdrawn_at",
        "withdrawal_reason",
        "mandatory_changed_at",
        "artifact_deleted_at",
        "created_at",
        "updated_at",
    ]
    assert columns.id.primary_key is True
    assert columns.id.default.arg.__wrapped__ is uuid.uuid4
    assert isinstance(columns.platform.type, String)
    assert columns.platform.type.length == 16
    assert isinstance(columns.channel.type, String)
    assert columns.channel.type.length == 32
    assert isinstance(columns.delivery_method.type, String)
    assert columns.delivery_method.type.length == 24
    assert isinstance(columns.version_code.type, Integer)
    assert isinstance(columns.version_name.type, String)
    assert columns.version_name.type.length == 64
    assert isinstance(columns.runtime_version.type, String)
    assert columns.runtime_version.type.length == 255
    assert isinstance(columns.fingerprint.type, String)
    assert columns.fingerprint.type.length == 128
    assert isinstance(columns.release_notes.type, JSONB)
    assert isinstance(columns.status.type, String)
    assert columns.status.type.length == 16
    assert isinstance(columns.is_mandatory.type, Boolean)
    assert columns.is_mandatory.server_default is not None
    assert isinstance(columns.artifact_size_bytes.type, BigInteger)
    assert isinstance(columns.source_commit.type, String)
    assert columns.source_commit.type.length == 40
    assert isinstance(columns.ci_run_id.type, String)
    assert columns.ci_run_id.type.length == 128
    assert isinstance(columns.idempotency_key.type, String)
    assert columns.idempotency_key.type.length == 128
    assert isinstance(columns.eas_build_id.type, String)
    assert columns.eas_build_id.type.length == 128
    assert isinstance(columns.eas_update_id.type, String)
    assert columns.eas_update_id.type.length == 128
    assert columns.eas_update_id.nullable is True
    assert isinstance(columns.eas_update_group_id.type, String)
    assert columns.eas_update_group_id.type.length == 128
    assert isinstance(columns.withdrawal_reason.type, String)
    assert columns.withdrawal_reason.type.length == 500
    for name in (
        "published_at",
        "withdrawn_at",
        "mandatory_changed_at",
        "artifact_deleted_at",
        "created_at",
        "updated_at",
    ):
        assert isinstance(columns[name].type, DateTime)
        assert columns[name].type.timezone is True
    assert columns.published_at.server_default is not None
    assert columns.created_at.server_default is not None
    assert columns.updated_at.server_default is not None


def test_release_metadata_has_unique_delivery_and_latest_indexes():
    """Catches duplicate binary/EAS releases or slow/wrong latest-release lookups."""
    constraint_names = {
        item.name for item in AppRelease.__table__.constraints if item.name
    }
    index_names = {item.name for item in AppRelease.__table__.indexes}

    assert "uq_app_releases_eas_update_group_id" in constraint_names
    assert "uq_app_releases_eas_update_id" in constraint_names
    assert "uq_app_releases_direct_version" in index_names
    assert "ix_app_releases_latest_published" in index_names


def test_release_domain_checks_match_the_persisted_contract():
    """Catches rows with unsupported targets or malformed published-release data."""
    lane_checks = {
        item.name: " ".join(str(item.sqltext).split())
        for item in AppReleaseLane.__table__.constraints
        if getattr(item, "sqltext", None) is not None
    }
    release_checks = {
        item.name: " ".join(str(item.sqltext).split())
        for item in AppRelease.__table__.constraints
        if getattr(item, "sqltext", None) is not None
    }

    assert lane_checks == {
        "ck_app_release_lanes_platform": "platform = 'android'",
        "ck_app_release_lanes_channel": (
            "channel IN ('production-direct', 'production-play')"
        ),
        "ck_app_release_lanes_expected_source_commit": (
            "expected_source_commit::text ~ '^[0-9a-f]{40}$'"
        ),
    }
    assert release_checks == {
        "ck_app_releases_platform": "platform = 'android'",
        "ck_app_releases_channel": "channel IN ('production-direct', 'production-play')",
        "ck_app_releases_delivery_method": (
            "delivery_method IN ('direct_apk', 'eas_update', 'google_play')"
        ),
        "ck_app_releases_version_code_positive": "version_code > 0",
        "ck_app_releases_version_name": (
            "version_name ~ '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$'"
        ),
        "ck_app_releases_min_supported_version": (
            "min_supported_version_code IS NULL OR min_supported_version_code <= version_code"
        ),
        "ck_app_releases_artifact_sha256": (
            "artifact_sha256 IS NULL OR artifact_sha256::text ~ '^[0-9a-f]{64}$'"
        ),
        "ck_app_releases_source_commit": "source_commit::text ~ '^[0-9a-f]{40}$'",
        "ck_app_releases_release_notes": (
            "release_notes ? 'ru' AND release_notes ? 'en' "
            "AND jsonb_typeof(release_notes) = 'object' "
            "AND jsonb_typeof(release_notes->'ru') = 'string' "
            "AND btrim(release_notes->>'ru') <> '' "
            "AND jsonb_typeof(release_notes->'en') = 'string' "
            "AND btrim(release_notes->>'en') <> ''"
        ),
        "ck_app_releases_delivery_payload": (
            "(delivery_method = 'direct_apk' AND artifact_storage_key IS NOT NULL "
            "AND artifact_sha256 IS NOT NULL AND artifact_size_bytes > 0) OR "
            "(delivery_method = 'eas_update' AND eas_update_group_id IS NOT NULL "
            "AND runtime_version IS NOT NULL AND artifact_storage_key IS NULL) OR "
            "(delivery_method = 'google_play' AND artifact_storage_key IS NULL)"
        ),
        "ck_app_releases_eas_update_id_delivery": (
            "delivery_method = 'eas_update' OR eas_update_id IS NULL"
        ),
        "ck_app_releases_non_apk_artifact_fields": (
            "delivery_method = 'direct_apk' OR "
            "(artifact_sha256 IS NULL AND artifact_size_bytes IS NULL)"
        ),
        "ck_app_releases_status": "status IN ('published', 'withdrawn')",
        "ck_app_releases_withdrawal_state": (
            "(status = 'published' AND withdrawn_at IS NULL AND withdrawal_reason IS NULL) OR "
            "(status = 'withdrawn' AND withdrawn_at IS NOT NULL AND withdrawal_reason IS NOT NULL)"
        ),
    }


def test_release_notes_check_explicitly_rejects_missing_language_keys():
    """Catches PostgreSQL CHECK-NULL acceptance when either required note key is absent."""
    release_notes_check = next(
        item
        for item in AppRelease.__table__.constraints
        if item.name == "ck_app_releases_release_notes"
    )
    sql = " ".join(str(release_notes_check.sqltext).split())

    assert "release_notes ? 'ru'" in sql
    assert "release_notes ? 'en'" in sql


def test_release_indexes_use_the_expected_postgresql_expressions_and_predicates():
    """Catches duplicate-release and latest-lookup index definitions that lose their predicates."""
    indexes = {item.name: item for item in AppRelease.__table__.indexes}
    direct = indexes["uq_app_releases_direct_version"]
    latest = indexes["ix_app_releases_latest_published"]

    assert direct.unique is True
    assert [str(expression) for expression in direct.expressions] == [
        "app_releases.platform",
        "app_releases.channel",
        "app_releases.version_code",
    ]
    assert str(direct.dialect_options["postgresql"]["where"]) == (
        "delivery_method = 'direct_apk'"
    )
    assert latest.unique is False
    assert [str(expression) for expression in latest.expressions] == [
        "app_releases.platform",
        "app_releases.channel",
        "version_code DESC",
        "published_at DESC",
    ]
    assert str(latest.dialect_options["postgresql"]["where"]) == "status = 'published'"


def test_release_migration_offline_sql_creates_and_removes_only_release_schema(monkeypatch, capsys):
    """Catches a migration that cannot expand and contract the release schema by itself."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))

    command.upgrade(config, "20260830_02:20260902_01", sql=True)
    upgrade_sql = capsys.readouterr().out
    assert "CREATE TABLE app_release_lanes" in upgrade_sql
    assert "CREATE TABLE app_releases" in upgrade_sql
    assert "uq_app_releases_direct_version" in upgrade_sql
    assert "ix_app_releases_latest_published" in upgrade_sql
    for constraint_name in (
        "ck_app_release_lanes_platform",
        "ck_app_release_lanes_channel",
        "ck_app_release_lanes_expected_source_commit",
        "ck_app_releases_channel",
        "ck_app_releases_source_commit",
        "ck_app_releases_artifact_sha256",
        "ck_app_releases_version_name",
        "ck_app_releases_release_notes",
    ):
        assert constraint_name in upgrade_sql
    assert (
        "CHECK (version_name ~ '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)"
        "\\.(0|[1-9][0-9]*)$')"
    ) in upgrade_sql
    assert (
        "CHECK (release_notes ? 'ru' AND release_notes ? 'en' "
        "AND jsonb_typeof(release_notes) = 'object'"
    ) in upgrade_sql

    command.downgrade(config, "20260902_01:20260830_02", sql=True)
    downgrade_sql = capsys.readouterr().out
    destructive_statements = [
        " ".join(line.split())
        for line in downgrade_sql.splitlines()
        if line.lstrip().upper().startswith(("DROP ", "ALTER "))
    ]
    assert destructive_statements == [
        "DROP INDEX ix_app_releases_latest_published;",
        "DROP INDEX uq_app_releases_direct_version;",
        "DROP TABLE app_releases;",
        "DROP TABLE app_release_lanes;",
    ]


def test_eas_update_id_migration_is_additive_and_reversible(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))

    command.upgrade(config, "20260902_01:20260906_01", sql=True)
    upgrade_sql = capsys.readouterr().out
    assert "ADD COLUMN eas_update_id VARCHAR(128)" in upgrade_sql
    assert "uq_app_releases_eas_update_id" in upgrade_sql
    assert "ck_app_releases_eas_update_id_delivery" in upgrade_sql

    command.downgrade(config, "20260906_01:20260902_01", sql=True)
    downgrade_sql = capsys.readouterr().out
    assert "DROP CONSTRAINT uq_app_releases_eas_update_id" in downgrade_sql
    assert "DROP COLUMN eas_update_id" in downgrade_sql
