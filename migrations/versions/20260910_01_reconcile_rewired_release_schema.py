"""Reconcile schema skipped by the historical 20260908_01 graph rewrite.

Production originally received ``20260908_01`` directly after ``20260822_02``.
That revision was later rewired after ``20260906_01``.  A database carrying the
original revision ID therefore appears to Alembic to have applied four schema
migrations which it never ran.  This forward-only migration inspects the real
schema and replays only a completely absent additive change.  Complete prior
applications are no-ops; partial or incompatible objects abort the transaction.

Revision ID: 20260910_01
Revises: 20260909_02
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260910_01"
down_revision: str | None = "20260909_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class SchemaReconciliationError(RuntimeError):
    """The database is neither safely pre-change nor exactly post-change."""


_NOTIFICATION_COLUMNS = {
    "message_key": ("varchar", 128, True, None),
    "message_params": ("jsonb", None, True, None),
}
_EXERCISE_COLUMNS = {
    "name_en": ("varchar", 200, True, None),
    "description_en": ("text", None, True, None),
}
_LANE_COLUMNS = {
    "platform": ("varchar", 16, False, None),
    "channel": ("varchar", 32, False, None),
    "expected_source_commit": ("char", 40, False, None),
    "expected_ci_run_id": ("varchar", 128, True, None),
    "updated_at": ("timestamptz", None, False, "now()"),
}
_RELEASE_BASE_COLUMNS = {
    "id": ("uuid", None, False, None),
    "platform": ("varchar", 16, False),
    "channel": ("varchar", 32, False),
    "delivery_method": ("varchar", 24, False),
    "version_code": ("integer", None, False),
    "version_name": ("varchar", 64, False),
    "runtime_version": ("varchar", 255, True),
    "fingerprint": ("varchar", 128, True),
    "release_notes": ("jsonb", None, False),
    "status": ("varchar", 16, False, "'published'"),
    "is_mandatory": ("boolean", None, False, "false"),
    "min_supported_version_code": ("integer", None, True),
    "artifact_storage_key": ("varchar", 255, True),
    "artifact_sha256": ("char", 64, True),
    "artifact_size_bytes": ("bigint", None, True),
    "source_commit": ("char", 40, False),
    "ci_run_id": ("varchar", 128, False),
    "idempotency_key": ("varchar", 128, False),
    "eas_build_id": ("varchar", 128, True),
    "eas_update_group_id": ("varchar", 128, True),
    "published_at": ("timestamptz", None, False, "now()"),
    "withdrawn_at": ("timestamptz", None, True),
    "withdrawal_reason": ("varchar", 500, True),
    "mandatory_changed_at": ("timestamptz", None, True),
    "artifact_deleted_at": ("timestamptz", None, True),
    "created_at": ("timestamptz", None, False, "now()"),
    "updated_at": ("timestamptz", None, False, "now()"),
}
for _column_name, _signature in tuple(_RELEASE_BASE_COLUMNS.items()):
    if len(_signature) == 3:
        _RELEASE_BASE_COLUMNS[_column_name] = (*_signature, None)
_LANE_CHECKS = {
    "ck_app_release_lanes_platform": "platform = 'android'",
    "ck_app_release_lanes_channel": "channel IN ('production-direct', 'production-play')",
    "ck_app_release_lanes_expected_source_commit": "expected_source_commit::text ~ '^[0-9a-f]{40}$'",
}
_RELEASE_BASE_CHECKS = {
    "ck_app_releases_platform": "platform = 'android'",
    "ck_app_releases_channel": "channel IN ('production-direct', 'production-play')",
    "ck_app_releases_delivery_method": "delivery_method IN ('direct_apk', 'eas_update', 'google_play')",
    "ck_app_releases_version_code_positive": "version_code > 0",
    "ck_app_releases_version_name": "version_name ~ '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$'",
    "ck_app_releases_min_supported_version": "min_supported_version_code IS NULL OR min_supported_version_code <= version_code",
    "ck_app_releases_artifact_sha256": "artifact_sha256 IS NULL OR artifact_sha256::text ~ '^[0-9a-f]{64}$'",
    "ck_app_releases_source_commit": "source_commit::text ~ '^[0-9a-f]{40}$'",
    "ck_app_releases_release_notes": "release_notes ? 'ru' AND release_notes ? 'en' AND jsonb_typeof(release_notes) = 'object' AND jsonb_typeof(release_notes->'ru') = 'string' AND btrim(release_notes->>'ru') <> '' AND jsonb_typeof(release_notes->'en') = 'string' AND btrim(release_notes->>'en') <> ''",
    "ck_app_releases_delivery_payload": "(delivery_method = 'direct_apk' AND artifact_storage_key IS NOT NULL AND artifact_sha256 IS NOT NULL AND artifact_size_bytes > 0) OR (delivery_method = 'eas_update' AND eas_update_group_id IS NOT NULL AND runtime_version IS NOT NULL AND artifact_storage_key IS NULL) OR (delivery_method = 'google_play' AND artifact_storage_key IS NULL)",
    "ck_app_releases_non_apk_artifact_fields": "delivery_method = 'direct_apk' OR (artifact_sha256 IS NULL AND artifact_size_bytes IS NULL)",
    "ck_app_releases_status": "status IN ('published', 'withdrawn')",
    "ck_app_releases_withdrawal_state": "(status = 'published' AND withdrawn_at IS NULL AND withdrawal_reason IS NULL) OR (status = 'withdrawn' AND withdrawn_at IS NOT NULL AND withdrawal_reason IS NOT NULL)",
}
_EAS_CHECK = "delivery_method = 'eas_update' OR eas_update_id IS NULL"


def _type_signature(type_: Any) -> tuple[str, int | None]:
    if isinstance(type_, postgresql.JSONB):
        return ("jsonb", None)
    if isinstance(type_, sa.CHAR):
        return ("char", type_.length)
    if isinstance(type_, sa.Text):
        return ("text", None)
    if isinstance(type_, sa.String):
        return ("varchar", type_.length)
    if isinstance(type_, sa.BigInteger):
        return ("bigint", None)
    if isinstance(type_, sa.Integer):
        return ("integer", None)
    if isinstance(type_, sa.Boolean):
        return ("boolean", None)
    if isinstance(type_, sa.DateTime) and type_.timezone:
        return ("timestamptz", None)
    if isinstance(type_, sa.UUID):
        return ("uuid", None)
    return (type(type_).__name__.lower(), getattr(type_, "length", None))


def _sql_signature(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value)
    pieces: list[str] = []
    index = 0
    in_literal = False
    while index < len(raw):
        character = raw[index]
        if in_literal:
            pieces.append(character)
            if character == "'":
                if index + 1 < len(raw) and raw[index + 1] == "'":
                    pieces.append("'")
                    index += 1
                else:
                    in_literal = False
        elif character == "'":
            in_literal = True
            pieces.append(character)
        elif not character.isspace():
            pieces.append(character.lower())
        index += 1
    if in_literal:
        raise SchemaReconciliationError("unterminated SQL literal")
    normalized = "".join(pieces)
    normalized = re.sub(r"::(?:text|charactervarying|boolean|integer|bigint)", "", normalized)
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        enclosed = True
        for index, character in enumerate(normalized):
            depth += character == "("
            depth -= character == ")"
            if depth == 0 and index != len(normalized) - 1:
                enclosed = False
                break
        if not enclosed:
            break
        normalized = normalized[1:-1]
    return normalized


def _columns(inspector: Any, table: str) -> dict[str, tuple[str, int | None, bool, str | None]]:
    return {
        item["name"]: (*_type_signature(item["type"]), bool(item["nullable"]), _sql_signature(item.get("default")))
        for item in inspector.get_columns(table)
    }


def _require_exact_columns(
    inspector: Any,
    table: str,
    expected: Mapping[str, tuple[str, int | None, bool, str | None]],
) -> None:
    actual = _columns(inspector, table)
    if actual != dict(expected):
        raise SchemaReconciliationError(
            f"incompatible {table} columns: expected {sorted(expected)}, "
            f"found {sorted(actual)}"
        )


def _named(items: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(item["name"]) for item in items if item.get("name")}


def _require_exact_checks(inspector: Any, table: str, expected: Mapping[str, str]) -> None:
    checks = inspector.get_check_constraints(table)
    if any(not item.get("name") or item.get("sqltext") is None for item in checks):
        raise SchemaReconciliationError(f"incompatible {table} check constraints")
    actual = {
        str(item.get("name")): _sql_signature(item.get("sqltext"))
        for item in checks
    }
    wanted = {name: _sql_signature(sqltext) for name, sqltext in expected.items()}
    if actual != wanted:
        raise SchemaReconciliationError(f"incompatible {table} check constraints")


def _predicate_signature(item: Mapping[str, Any]) -> str:
    return _sql_signature(
        (item.get("dialect_options") or {}).get("postgresql_where")
    ) or ""


def _plan_optional_columns(
    inspector: Any,
    *,
    table: str,
    expected: Mapping[str, tuple[str, int | None, bool, str | None]],
    action: str,
) -> list[str]:
    actual = _columns(inspector, table)
    present = set(actual).intersection(expected)
    if not present:
        return [action]
    if present != set(expected):
        raise SchemaReconciliationError(f"partial {action} columns")
    for name, signature in expected.items():
        if actual[name] != signature:
            raise SchemaReconciliationError(f"incompatible {table}.{name}")
    return []


def _validate_release_base(inspector: Any) -> None:
    _require_exact_columns(inspector, "app_release_lanes", _LANE_COLUMNS)
    release_columns = _columns(inspector, "app_releases")
    allowed = dict(_RELEASE_BASE_COLUMNS)
    if "eas_update_id" in release_columns:
        allowed["eas_update_id"] = ("varchar", 128, True, None)
    if release_columns != allowed:
        raise SchemaReconciliationError("incompatible app_releases columns")

    if inspector.get_pk_constraint("app_release_lanes").get("constrained_columns") != [
        "platform",
        "channel",
    ]:
        raise SchemaReconciliationError("incompatible app_release_lanes primary key")
    if inspector.get_pk_constraint("app_releases").get("constrained_columns") != ["id"]:
        raise SchemaReconciliationError("incompatible app_releases primary key")
    _require_exact_checks(inspector, "app_release_lanes", _LANE_CHECKS)
    release_checks = dict(_RELEASE_BASE_CHECKS)
    if "eas_update_id" in release_columns:
        release_checks["ck_app_releases_eas_update_id_delivery"] = _EAS_CHECK
    _require_exact_checks(inspector, "app_releases", release_checks)
    if inspector.get_foreign_keys("app_release_lanes") or inspector.get_foreign_keys("app_releases"):
        raise SchemaReconciliationError("unexpected release foreign key")
    if inspector.get_unique_constraints("app_release_lanes") or inspector.get_indexes("app_release_lanes"):
        raise SchemaReconciliationError("unexpected app_release_lanes index or unique constraint")

    uniques = {
        item.get("name"): tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints("app_releases")
    }
    expected_uniques = {
        "uq_app_releases_idempotency_key": ("idempotency_key",),
        "uq_app_releases_eas_update_group_id": ("eas_update_group_id",),
    }
    if "eas_update_id" in release_columns:
        expected_uniques["uq_app_releases_eas_update_id"] = ("eas_update_id",)
    if uniques != expected_uniques:
        raise SchemaReconciliationError("incompatible release unique constraints")
    index_items = [
        item
        for item in inspector.get_indexes("app_releases")
        if not item.get("duplicates_constraint")
    ]
    for item in index_items:
        dialect_options = item.get("dialect_options") or {}
        if set(dialect_options) - {"postgresql_where"}:
            raise SchemaReconciliationError("incompatible release index options")
    indexes = {
        item.get("name"): (
            bool(item.get("unique")),
            tuple(item.get("column_names") or ()),
            _predicate_signature(item),
            tuple(
                sorted(
                    (str(column), tuple(options))
                    for column, options in (item.get("column_sorting") or {}).items()
                )
            ),
        )
        for item in index_items
    }
    expected_indexes = {
        "uq_app_releases_direct_version": (True, ("platform", "channel", "version_code"), "delivery_method='direct_apk'", ()),
        "ix_app_releases_latest_published": (
            False,
            ("platform", "channel", "version_code", "published_at"),
            "status='published'",
            (("published_at", ("desc",)), ("version_code", ("desc",))),
        ),
    }
    if indexes != expected_indexes:
        raise SchemaReconciliationError("incompatible release indexes")


def _plan_eas_extension(inspector: Any) -> list[str]:
    columns = _columns(inspector, "app_releases")
    has_column = "eas_update_id" in columns
    unique_items = inspector.get_unique_constraints("app_releases")
    uniques = _named(unique_items)
    checks = _named(inspector.get_check_constraints("app_releases"))
    state = (
        has_column,
        "uq_app_releases_eas_update_id" in uniques,
        "ck_app_releases_eas_update_id_delivery" in checks,
    )
    if state == (False, False, False):
        return ["eas_update_id"]
    if state != (True, True, True):
        raise SchemaReconciliationError("partial EAS update ID schema")
    if columns["eas_update_id"] != ("varchar", 128, True, None):
        raise SchemaReconciliationError("incompatible app_releases.eas_update_id")
    eas_unique = next(
        item for item in unique_items if item.get("name") == "uq_app_releases_eas_update_id"
    )
    if eas_unique.get("column_names") != ["eas_update_id"]:
        raise SchemaReconciliationError("incompatible EAS update ID unique constraint")
    return []


def plan_reconciliation(inspector: Any) -> tuple[str, ...]:
    """Return safe additive migrations after validating all relevant objects."""
    tables = set(inspector.get_table_names())
    for required in ("app_notifications", "exercises"):
        if required not in tables:
            raise SchemaReconciliationError(f"required baseline table missing: {required}")

    actions = _plan_optional_columns(
        inspector,
        table="app_notifications",
        expected=_NOTIFICATION_COLUMNS,
        action="notification_message_keys",
    )

    exercise_actions = _plan_optional_columns(
        inspector,
        table="exercises",
        expected=_EXERCISE_COLUMNS,
        action="exercise_localizations",
    )
    exercise_index = next(
        (
            item
            for item in inspector.get_indexes("exercises")
            if item.get("name") == "ix_exercises_name_en"
        ),
        None,
    )
    if exercise_actions and exercise_index is not None:
        raise SchemaReconciliationError("localization index exists without its columns")
    if not exercise_actions:
        if (
            exercise_index is None
            or exercise_index.get("column_names") != ["name_en"]
            or bool(exercise_index.get("unique"))
        ):
            raise SchemaReconciliationError("missing or incompatible localization index")
    actions.extend(exercise_actions)

    release_tables = tables.intersection({"app_release_lanes", "app_releases"})
    if not release_tables:
        actions.extend(("release_registry", "eas_update_id"))
        return tuple(actions)
    if release_tables != {"app_release_lanes", "app_releases"}:
        raise SchemaReconciliationError("partial release registry tables")

    _validate_release_base(inspector)
    actions.extend(_plan_eas_extension(inspector))
    return tuple(actions)


def _default_migrations() -> dict[str, Callable[[], None]]:
    modules = {
        "notification_message_keys": "20260830_01_notification_message_keys",
        "exercise_localizations": "20260830_02_exercise_localizations",
        "release_registry": "20260902_01_app_releases",
        "eas_update_id": "20260906_01_eas_update_id",
    }
    return {
        action: importlib.import_module(f"migrations.versions.{module}").upgrade
        for action, module in modules.items()
    }


def apply_reconciliation(
    actions: Sequence[str],
    *,
    migrations: Mapping[str, Callable[[], None]] | None = None,
) -> None:
    implementations = _default_migrations() if migrations is None else migrations
    for action in actions:
        try:
            implementation = implementations[action]
        except KeyError as exc:
            raise SchemaReconciliationError(f"unknown reconciliation action: {action}") from exc
        implementation()


def upgrade() -> None:
    actions = plan_reconciliation(sa.inspect(op.get_bind()))
    apply_reconciliation(actions)


def downgrade() -> None:
    # Forward-only repair. Removing these additive objects could destroy release
    # records or localization/notification data written after reconciliation.
    pass
