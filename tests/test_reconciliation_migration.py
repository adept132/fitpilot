import importlib

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


MIGRATION_MODULE = (
    "migrations.versions.20260910_01_reconcile_rewired_release_schema"
)


class FakeInspector:
    def __init__(self, *, tables, columns, indexes=None, uniques=None, checks=None, pks=None):
        self._tables = set(tables)
        self._columns = columns
        self._indexes = indexes or {}
        self._uniques = uniques or {}
        self._checks = checks or {}
        self._pks = pks or {}

    def get_table_names(self):
        return sorted(self._tables)

    def get_columns(self, table):
        return self._columns[table]

    def get_indexes(self, table):
        return self._indexes.get(table, [])

    def get_unique_constraints(self, table):
        return self._uniques.get(table, [])

    def get_check_constraints(self, table):
        return self._checks.get(table, [])

    def get_pk_constraint(self, table):
        return self._pks.get(table, {"constrained_columns": []})


def _column(name, type_, nullable):
    return {"name": name, "type": type_, "nullable": nullable}


def _release_schema(*, include_eas=True):
    lane_columns = [
        _column("platform", sa.String(16), False),
        _column("channel", sa.String(32), False),
        _column("expected_source_commit", sa.CHAR(40), False),
        _column("expected_ci_run_id", sa.String(128), True),
        _column("updated_at", sa.DateTime(timezone=True), False),
    ]
    release_columns = [
        _column("id", postgresql.UUID(), False),
        _column("platform", sa.String(16), False),
        _column("channel", sa.String(32), False),
        _column("delivery_method", sa.String(24), False),
        _column("version_code", sa.Integer(), False),
        _column("version_name", sa.String(64), False),
        _column("runtime_version", sa.String(255), True),
        _column("fingerprint", sa.String(128), True),
        _column("release_notes", postgresql.JSONB(), False),
        _column("status", sa.String(16), False),
        _column("is_mandatory", sa.Boolean(), False),
        _column("min_supported_version_code", sa.Integer(), True),
        _column("artifact_storage_key", sa.String(255), True),
        _column("artifact_sha256", sa.CHAR(64), True),
        _column("artifact_size_bytes", sa.BigInteger(), True),
        _column("source_commit", sa.CHAR(40), False),
        _column("ci_run_id", sa.String(128), False),
        _column("idempotency_key", sa.String(128), False),
        _column("eas_build_id", sa.String(128), True),
        _column("eas_update_group_id", sa.String(128), True),
        _column("published_at", sa.DateTime(timezone=True), False),
        _column("withdrawn_at", sa.DateTime(timezone=True), True),
        _column("withdrawal_reason", sa.String(500), True),
        _column("mandatory_changed_at", sa.DateTime(timezone=True), True),
        _column("artifact_deleted_at", sa.DateTime(timezone=True), True),
        _column("created_at", sa.DateTime(timezone=True), False),
        _column("updated_at", sa.DateTime(timezone=True), False),
    ]
    uniques = [
        {"name": "uq_app_releases_idempotency_key", "column_names": ["idempotency_key"]},
        {"name": "uq_app_releases_eas_update_group_id", "column_names": ["eas_update_group_id"]},
    ]
    checks = [
        {"name": name}
        for name in (
            "ck_app_releases_platform",
            "ck_app_releases_channel",
            "ck_app_releases_delivery_method",
            "ck_app_releases_version_code_positive",
            "ck_app_releases_version_name",
            "ck_app_releases_min_supported_version",
            "ck_app_releases_artifact_sha256",
            "ck_app_releases_source_commit",
            "ck_app_releases_release_notes",
            "ck_app_releases_delivery_payload",
            "ck_app_releases_non_apk_artifact_fields",
            "ck_app_releases_status",
            "ck_app_releases_withdrawal_state",
        )
    ]
    if include_eas:
        release_columns.insert(20, _column("eas_update_id", sa.String(128), True))
        uniques.append(
            {"name": "uq_app_releases_eas_update_id", "column_names": ["eas_update_id"]}
        )
        checks.append({"name": "ck_app_releases_eas_update_id_delivery"})
    return {
        "tables": {"app_notifications", "exercises", "app_release_lanes", "app_releases"},
        "columns": {
            "app_notifications": [
                _column("message_key", sa.String(128), True),
                _column("message_params", postgresql.JSONB(), True),
            ],
            "exercises": [
                _column("name_en", sa.String(200), True),
                _column("description_en", sa.Text(), True),
            ],
            "app_release_lanes": lane_columns,
            "app_releases": release_columns,
        },
        "indexes": {
            "exercises": [
                {"name": "ix_exercises_name_en", "column_names": ["name_en"], "unique": False}
            ],
            "app_releases": [
                {
                    "name": "uq_app_releases_direct_version",
                    "column_names": ["platform", "channel", "version_code"],
                    "unique": True,
                    "dialect_options": {
                        "postgresql_where": "(delivery_method = 'direct_apk'::text)"
                    },
                },
                {
                    "name": "ix_app_releases_latest_published",
                    "column_names": [
                        "platform",
                        "channel",
                        "version_code",
                        "published_at",
                    ],
                    "unique": False,
                    "dialect_options": {
                        "postgresql_where": "(status = 'published'::text)"
                    },
                },
            ],
        },
        "uniques": {
            "app_releases": uniques,
        },
        "checks": {
            "app_release_lanes": [
                {"name": "ck_app_release_lanes_platform"},
                {"name": "ck_app_release_lanes_channel"},
                {"name": "ck_app_release_lanes_expected_source_commit"},
            ],
            "app_releases": checks,
        },
        "pks": {
            "app_release_lanes": {"constrained_columns": ["platform", "channel"]},
            "app_releases": {"constrained_columns": ["id"]},
        },
    }


def _prod_like_inspector():
    return FakeInspector(
        tables={"app_notifications", "exercises"},
        columns={"app_notifications": [], "exercises": []},
    )


def test_prod_like_rewired_history_replays_all_skipped_schema_changes():
    migration = importlib.import_module(MIGRATION_MODULE)

    actions = migration.plan_reconciliation(_prod_like_inspector())

    assert actions == (
        "notification_message_keys",
        "exercise_localizations",
        "release_registry",
        "eas_update_id",
    )


def test_history_where_old_migrations_ran_is_an_idempotent_noop():
    migration = importlib.import_module(MIGRATION_MODULE)

    actions = migration.plan_reconciliation(FakeInspector(**_release_schema()))

    assert actions == ()


def test_reconciliation_executes_the_original_additive_migrations_in_order():
    migration = importlib.import_module(MIGRATION_MODULE)
    called = []
    migrations = {
        action: (lambda action=action: called.append(action))
        for action in (
            "notification_message_keys",
            "exercise_localizations",
            "release_registry",
            "eas_update_id",
        )
    }

    migration.apply_reconciliation(
        (
            "notification_message_keys",
            "exercise_localizations",
            "release_registry",
            "eas_update_id",
        ),
        migrations=migrations,
    )

    assert called == list(migrations)


def test_release_registry_without_eas_extension_only_adds_eas_extension():
    migration = importlib.import_module(MIGRATION_MODULE)

    actions = migration.plan_reconciliation(
        FakeInspector(**_release_schema(include_eas=False))
    )

    assert actions == ("eas_update_id",)


@pytest.mark.parametrize(
    "inspector",
    [
        FakeInspector(
            tables={"app_notifications", "exercises"},
            columns={
                "app_notifications": [_column("message_key", sa.String(128), True)],
                "exercises": [],
            },
        ),
        FakeInspector(
            tables={"app_notifications", "exercises", "app_releases"},
            columns={"app_notifications": [], "exercises": [], "app_releases": []},
        ),
    ],
)
def test_incompatible_partial_schema_fails_closed(inspector):
    migration = importlib.import_module(MIGRATION_MODULE)

    with pytest.raises(migration.SchemaReconciliationError):
        migration.plan_reconciliation(inspector)


def test_existing_localization_index_with_wrong_uniqueness_fails_closed():
    migration = importlib.import_module(MIGRATION_MODULE)
    schema = _release_schema()
    schema["indexes"]["exercises"][0]["unique"] = True

    with pytest.raises(migration.SchemaReconciliationError):
        migration.plan_reconciliation(FakeInspector(**schema))


def test_existing_release_index_on_wrong_columns_fails_closed():
    migration = importlib.import_module(MIGRATION_MODULE)
    schema = _release_schema()
    schema["indexes"]["app_releases"][0]["column_names"] = ["platform"]

    with pytest.raises(migration.SchemaReconciliationError):
        migration.plan_reconciliation(FakeInspector(**schema))


def test_existing_release_index_with_wrong_predicate_fails_closed():
    migration = importlib.import_module(MIGRATION_MODULE)
    schema = _release_schema()
    schema["indexes"]["app_releases"][0]["dialect_options"] = {
        "postgresql_where": "delivery_method = 'google_play'"
    }

    with pytest.raises(migration.SchemaReconciliationError):
        migration.plan_reconciliation(FakeInspector(**schema))


def test_existing_eas_constraint_on_wrong_column_fails_closed():
    migration = importlib.import_module(MIGRATION_MODULE)
    schema = _release_schema()
    eas_unique = next(
        item
        for item in schema["uniques"]["app_releases"]
        if item["name"] == "uq_app_releases_eas_update_id"
    )
    eas_unique["column_names"] = ["eas_update_group_id"]

    with pytest.raises(migration.SchemaReconciliationError):
        migration.plan_reconciliation(FakeInspector(**schema))


def test_reconciliation_is_forward_only_after_security_cleanup_head():
    migration = importlib.import_module(MIGRATION_MODULE)

    assert migration.revision == "20260910_01"
    assert migration.down_revision == "20260909_02"
    assert migration.downgrade() is None
