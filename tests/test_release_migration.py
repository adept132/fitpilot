import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from api.services.models import AppRelease, AppReleaseLane


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
    assert "uq_app_releases_direct_version" in index_names
    assert "ix_app_releases_latest_published" in index_names


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

    command.downgrade(config, "20260902_01:20260830_02", sql=True)
    downgrade_sql = capsys.readouterr().out
    assert "DROP INDEX ix_app_releases_latest_published" in downgrade_sql
    assert "DROP INDEX uq_app_releases_direct_version" in downgrade_sql
    assert "DROP TABLE app_releases" in downgrade_sql
    assert "DROP TABLE app_release_lanes" in downgrade_sql
