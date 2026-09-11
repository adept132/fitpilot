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
_PG_REFLECTED_CHECKS = {
    "ck_app_release_lanes_platform": "platform::text = 'android'::text",
    "ck_app_release_lanes_channel": "channel::text = ANY (ARRAY['production-direct'::character varying, 'production-play'::character varying]::text[])",
    "ck_app_release_lanes_expected_source_commit": "expected_source_commit::text ~ '^[0-9a-f]{40}$'::text",
    "ck_app_releases_platform": "platform::text = 'android'::text",
    "ck_app_releases_channel": "channel::text = ANY (ARRAY['production-direct'::character varying, 'production-play'::character varying]::text[])",
    "ck_app_releases_delivery_method": "delivery_method::text = ANY (ARRAY['direct_apk'::character varying, 'eas_update'::character varying, 'google_play'::character varying]::text[])",
    "ck_app_releases_version_code_positive": "version_code > 0",
    "ck_app_releases_version_name": "version_name::text ~ '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$'::text",
    "ck_app_releases_min_supported_version": "min_supported_version_code IS NULL OR min_supported_version_code <= version_code",
    "ck_app_releases_artifact_sha256": "artifact_sha256 IS NULL OR artifact_sha256::text ~ '^[0-9a-f]{64}$'::text",
    "ck_app_releases_source_commit": "source_commit::text ~ '^[0-9a-f]{40}$'::text",
    "ck_app_releases_release_notes": "release_notes ? 'ru'::text AND release_notes ? 'en'::text AND jsonb_typeof(release_notes) = 'object'::text AND jsonb_typeof(release_notes -> 'ru'::text) = 'string'::text AND btrim(release_notes ->> 'ru'::text) <> ''::text AND jsonb_typeof(release_notes -> 'en'::text) = 'string'::text AND btrim(release_notes ->> 'en'::text) <> ''::text",
    "ck_app_releases_delivery_payload": "delivery_method::text = 'direct_apk'::text AND artifact_storage_key IS NOT NULL AND artifact_sha256 IS NOT NULL AND artifact_size_bytes > 0 OR delivery_method::text = 'eas_update'::text AND eas_update_group_id IS NOT NULL AND runtime_version IS NOT NULL AND artifact_storage_key IS NULL OR delivery_method::text = 'google_play'::text AND artifact_storage_key IS NULL",
    "ck_app_releases_non_apk_artifact_fields": "delivery_method::text = 'direct_apk'::text OR artifact_sha256 IS NULL AND artifact_size_bytes IS NULL",
    "ck_app_releases_status": "status::text = ANY (ARRAY['published'::character varying, 'withdrawn'::character varying]::text[])",
    "ck_app_releases_withdrawal_state": "status::text = 'published'::text AND withdrawn_at IS NULL AND withdrawal_reason IS NULL OR status::text = 'withdrawn'::text AND withdrawn_at IS NOT NULL AND withdrawal_reason IS NOT NULL",
    "ck_app_releases_eas_update_id_delivery": "delivery_method::text = 'eas_update'::text OR eas_update_id IS NULL",
}


def _pg_column_contract(
    columns: Mapping[str, tuple[str, int | None, bool, str | None]]
) -> dict[str, tuple[str, bool, str | None, str | None]]:
    names = {
        "varchar": "character varying",
        "char": "character",
        "timestamptz": "timestamp with time zone",
    }
    contract = {}
    for name, (kind, length, nullable, default) in columns.items():
        type_name = names.get(kind, kind)
        if length is not None:
            type_name += f"({length})"
        collation = "default" if kind in {"varchar", "char"} else None
        contract[name] = (type_name, not nullable, default, collation)
    return contract


_PG_LANE_COLUMNS = _pg_column_contract(_LANE_COLUMNS)
_PG_RELEASE_BASE_COLUMNS = _pg_column_contract(_RELEASE_BASE_COLUMNS)
_PG_RELEASE_INDEXES = {
    "app_releases_pkey": (True, True, True, True, 1, 1, "CREATE UNIQUE INDEX app_releases_pkey ON app_releases USING btree (id)", None, None),
    "ix_app_releases_latest_published": (True, True, False, False, 4, 4, "CREATE INDEX ix_app_releases_latest_published ON app_releases USING btree (platform, channel, version_code DESC, published_at DESC) WHERE status::text = 'published'::text", "status::text = 'published'::text", None),
    "uq_app_releases_direct_version": (True, True, True, False, 3, 3, "CREATE UNIQUE INDEX uq_app_releases_direct_version ON app_releases USING btree (platform, channel, version_code) WHERE delivery_method::text = 'direct_apk'::text", "delivery_method::text = 'direct_apk'::text", None),
    "uq_app_releases_eas_update_group_id": (True, True, True, False, 1, 1, "CREATE UNIQUE INDEX uq_app_releases_eas_update_group_id ON app_releases USING btree (eas_update_group_id)", None, None),
    "uq_app_releases_idempotency_key": (True, True, True, False, 1, 1, "CREATE UNIQUE INDEX uq_app_releases_idempotency_key ON app_releases USING btree (idempotency_key)", None, None),
}
_PG_LANE_INDEXES = {
    "app_release_lanes_pkey": (True, True, True, True, 2, 2, "CREATE UNIQUE INDEX app_release_lanes_pkey ON app_release_lanes USING btree (platform, channel)", None, None),
}


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
    normalized = re.sub(r"\(([a-z_][a-z0-9_]*)\)", r"\1", normalized)
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
    if any(
        not item.get("name")
        or item.get("sqltext") is None
        or bool(item.get("dialect_options"))
        for item in checks
    ):
        raise SchemaReconciliationError(f"incompatible {table} check constraints")
    actual = {
        str(item.get("name")): _sql_signature(item.get("sqltext"))
        for item in checks
    }
    if set(actual) != set(expected):
        raise SchemaReconciliationError(f"incompatible {table} check constraints")
    for name, sqltext in expected.items():
        allowed = {
            _sql_signature(sqltext),
            _sql_signature(_PG_REFLECTED_CHECKS[name]),
        }
        if actual[name] not in allowed:
            raise SchemaReconciliationError(f"incompatible {table} check constraints")


def _predicate_signature(item: Mapping[str, Any]) -> str:
    return _sql_signature(
        (item.get("dialect_options") or {}).get("postgresql_where")
    ) or ""


def _has_only_default_index_options(
    item: Mapping[str, Any], *, allow_predicate: bool
) -> bool:
    options = dict(item.get("dialect_options") or {})
    include = options.pop("postgresql_include", [])
    predicate = options.pop("postgresql_where", None) if allow_predicate else None
    if include or options:
        return False
    if not allow_predicate and "postgresql_where" in (item.get("dialect_options") or {}):
        return False
    return predicate is None or allow_predicate


def _has_only_default_constraint_options(
    item: Mapping[str, Any], *, unique: bool = False
) -> bool:
    options = dict(item.get("dialect_options") or {})
    if options.pop("postgresql_include", []):
        return False
    if unique and options.pop("postgresql_nulls_not_distinct", False):
        return False
    return not options


def _catalog_rows(bind: Any, sql: str) -> list[Mapping[str, Any]]:
    return list(bind.execute(sa.text(sql)).mappings())


def _require_postgresql_catalog_contract(bind: Any, *, include_eas: bool) -> None:
    version = int(bind.execute(sa.text("SHOW server_version_num")).scalar_one())
    if not 160000 <= version < 180000:
        raise SchemaReconciliationError("unsupported PostgreSQL catalog version")

    column_rows = _catalog_rows(
        bind,
        """
        SELECT c.relname AS table_name, a.attname AS column_name,
               format_type(a.atttypid, a.atttypmod) AS type_name,
               a.attnotnull AS not_null, pg_get_expr(d.adbin, d.adrelid) AS default_sql,
               coll.collname AS collation_name
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        LEFT JOIN pg_collation coll ON coll.oid = a.attcollation
        WHERE n.nspname = current_schema()
          AND c.relname IN ('app_release_lanes', 'app_releases')
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
        """,
    )
    actual_columns: dict[str, dict[str, tuple[Any, ...]]] = {}
    for row in column_rows:
        actual_columns.setdefault(row["table_name"], {})[row["column_name"]] = (
            row["type_name"],
            bool(row["not_null"]),
            _sql_signature(row["default_sql"]),
            row["collation_name"],
        )
    release_columns = dict(_PG_RELEASE_BASE_COLUMNS)
    if include_eas:
        release_columns["eas_update_id"] = ("character varying(128)", False, None, "default")
    expected_columns = {
        "app_release_lanes": _PG_LANE_COLUMNS,
        "app_releases": release_columns,
    }
    expected_columns = {
        table: {
            name: (type_name, not_null, _sql_signature(default), collation)
            for name, (type_name, not_null, default, collation) in columns.items()
        }
        for table, columns in expected_columns.items()
    }
    if actual_columns != expected_columns:
        raise SchemaReconciliationError("incompatible release column catalog")

    constraint_rows = _catalog_rows(
        bind,
        """
        SELECT c.relname AS table_name, con.conname, con.contype,
               con.convalidated, con.connoinherit, con.condeferrable, con.condeferred,
               pg_get_constraintdef(con.oid, true) AS definition
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relname IN ('app_release_lanes', 'app_releases')
        ORDER BY c.relname, con.conname
        """,
    )
    actual_constraints: dict[str, dict[str, tuple[Any, ...]]] = {}
    for row in constraint_rows:
        actual_constraints.setdefault(row["table_name"], {})[row["conname"]] = (
            row["contype"], bool(row["convalidated"]), bool(row["connoinherit"]),
            bool(row["condeferrable"]), bool(row["condeferred"]), row["definition"],
        )
    lane_constraints = {
        name: ("c", True, False, False, False, f"CHECK ({_PG_REFLECTED_CHECKS[name]})")
        for name in _LANE_CHECKS
    }
    lane_constraints["app_release_lanes_pkey"] = (
        "p", True, True, False, False, "PRIMARY KEY (platform, channel)"
    )
    release_check_names = set(_RELEASE_BASE_CHECKS)
    if include_eas:
        release_check_names.add("ck_app_releases_eas_update_id_delivery")
    release_constraints = {
        name: ("c", True, False, False, False, f"CHECK ({_PG_REFLECTED_CHECKS[name]})")
        for name in release_check_names
    }
    release_constraints.update({
        "app_releases_pkey": ("p", True, True, False, False, "PRIMARY KEY (id)"),
        "uq_app_releases_eas_update_group_id": ("u", True, True, False, False, "UNIQUE (eas_update_group_id)"),
        "uq_app_releases_idempotency_key": ("u", True, True, False, False, "UNIQUE (idempotency_key)"),
    })
    if include_eas:
        release_constraints["uq_app_releases_eas_update_id"] = (
            "u", True, True, False, False, "UNIQUE (eas_update_id)"
        )
    if actual_constraints != {
        "app_release_lanes": lane_constraints,
        "app_releases": release_constraints,
    }:
        raise SchemaReconciliationError("incompatible release constraint catalog")

    index_rows = _catalog_rows(
        bind,
        """
        SELECT t.relname AS table_name, i.relname AS index_name,
               x.indisvalid, x.indisready, x.indisunique, x.indisprimary,
               x.indnkeyatts, x.indnatts, pg_get_indexdef(x.indexrelid, 0, true) AS definition,
               pg_get_expr(x.indpred, x.indrelid, true) AS predicate, i.reloptions
        FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_class t ON t.oid = x.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = current_schema()
          AND t.relname IN ('app_release_lanes', 'app_releases')
        ORDER BY t.relname, i.relname
        """,
    )
    actual_indexes: dict[str, dict[str, tuple[Any, ...]]] = {}
    for row in index_rows:
        actual_indexes.setdefault(row["table_name"], {})[row["index_name"]] = (
            bool(row["indisvalid"]), bool(row["indisready"]), bool(row["indisunique"]),
            bool(row["indisprimary"]), row["indnkeyatts"], row["indnatts"],
            row["definition"], row["predicate"], row["reloptions"],
        )
    release_indexes = dict(_PG_RELEASE_INDEXES)
    if include_eas:
        release_indexes["uq_app_releases_eas_update_id"] = (
            True, True, True, False, 1, 1,
            "CREATE UNIQUE INDEX uq_app_releases_eas_update_id ON app_releases USING btree (eas_update_id)",
            None, None,
        )
    if actual_indexes != {
        "app_release_lanes": _PG_LANE_INDEXES,
        "app_releases": release_indexes,
    }:
        raise SchemaReconciliationError("incompatible release index catalog")


def _require_exercise_index_catalog(bind: Any) -> None:
    rows = _catalog_rows(
        bind,
        """
        SELECT x.indisvalid, x.indisready, x.indisunique, x.indisprimary,
               x.indnkeyatts, x.indnatts, pg_get_indexdef(x.indexrelid, 0, true) AS definition,
               pg_get_expr(x.indpred, x.indrelid, true) AS predicate, i.reloptions
        FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_class t ON t.oid = x.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = current_schema() AND t.relname = 'exercises'
          AND i.relname = 'ix_exercises_name_en'
        """,
    )
    signature = tuple(rows[0].values()) if len(rows) == 1 else None
    expected = (True, True, False, False, 1, 1, "CREATE INDEX ix_exercises_name_en ON exercises USING btree (name_en)", None, None)
    if signature != expected:
        raise SchemaReconciliationError("incompatible localization index catalog")


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

    lane_pk = inspector.get_pk_constraint("app_release_lanes")
    release_pk = inspector.get_pk_constraint("app_releases")
    if lane_pk.get("constrained_columns") != [
        "platform",
        "channel",
    ] or not _has_only_default_constraint_options(lane_pk):
        raise SchemaReconciliationError("incompatible app_release_lanes primary key")
    if release_pk.get("constrained_columns") != ["id"] or not _has_only_default_constraint_options(release_pk):
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

    unique_items = inspector.get_unique_constraints("app_releases")
    if any(not _has_only_default_constraint_options(item, unique=True) for item in unique_items):
        raise SchemaReconciliationError("incompatible release unique constraints")
    uniques = {item.get("name"): tuple(item.get("column_names") or ()) for item in unique_items}
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
        if item.get("include_columns") or not _has_only_default_index_options(item, allow_predicate=True):
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


def plan_reconciliation(inspector: Any, *, bind: Any | None = None) -> tuple[str, ...]:
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
            or bool(exercise_index.get("column_sorting"))
            or bool(exercise_index.get("include_columns"))
            or not _has_only_default_index_options(exercise_index, allow_predicate=False)
        ):
            raise SchemaReconciliationError("missing or incompatible localization index")
        if bind is not None:
            _require_exercise_index_catalog(bind)
    actions.extend(exercise_actions)

    release_tables = tables.intersection({"app_release_lanes", "app_releases"})
    if not release_tables:
        actions.extend(("release_registry", "eas_update_id"))
        return tuple(actions)
    if release_tables != {"app_release_lanes", "app_releases"}:
        raise SchemaReconciliationError("partial release registry tables")

    _validate_release_base(inspector)
    eas_actions = _plan_eas_extension(inspector)
    if bind is not None:
        _require_postgresql_catalog_contract(bind, include_eas=not eas_actions)
    actions.extend(eas_actions)
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
    bind = op.get_bind()
    actions = plan_reconciliation(sa.inspect(bind), bind=bind)
    apply_reconciliation(actions)
    if plan_reconciliation(sa.inspect(bind), bind=bind):
        raise SchemaReconciliationError("reconciliation did not reach the exact target schema")


def downgrade() -> None:
    # Forward-only repair. Removing these additive objects could destroy release
    # records or localization/notification data written after reconciliation.
    pass
