#!/usr/bin/env python3
"""Fail-closed static gate for automatically deployable additive migrations."""

from __future__ import annotations

import ast
from pathlib import Path
import sys


ALLOWED_OP_CALLS = {
    "add_column",
    "create_check_constraint",
    "create_foreign_key",
    "create_index",
    "create_primary_key",
    "create_table",
    "create_unique_constraint",
}
ALLOWED_CONSTRUCTORS = {
    "ARRAY",
    "BigInteger",
    "Boolean",
    "CheckConstraint",
    "CHAR",
    "Column",
    "Date",
    "DateTime",
    "Enum",
    "Float",
    "ForeignKey",
    "ForeignKeyConstraint",
    "Index",
    "Integer",
    "JSON",
    "JSONB",
    "LargeBinary",
    "MetaData",
    "Numeric",
    "PrimaryKeyConstraint",
    "SmallInteger",
    "String",
    "Table",
    "Text",
    "Time",
    "UniqueConstraint",
    "UUID",
    "text",
}
CONSTRUCTOR_ROOTS = {"sa", "sqlalchemy", "postgresql"}
RESERVED_BINDINGS = {"op", "sa", "postgresql", "upgrade"}
SAFE_SQL_FRAGMENTS = {
    "now()",
    "'published'",
    "false",
    "platform = 'android'",
    "channel IN ('production-direct', 'production-play')",
    "expected_source_commit::text ~ '^[0-9a-f]{40}$'",
    "delivery_method IN ('direct_apk', 'eas_update', 'google_play')",
    "version_code > 0",
    "version_name ~ '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$'",
    "min_supported_version_code IS NULL OR min_supported_version_code <= version_code",
    "artifact_sha256 IS NULL OR artifact_sha256::text ~ '^[0-9a-f]{64}$'",
    "source_commit::text ~ '^[0-9a-f]{40}$'",
    "release_notes ? 'ru' AND release_notes ? 'en' AND jsonb_typeof(release_notes) = 'object' AND jsonb_typeof(release_notes->'ru') = 'string' AND btrim(release_notes->>'ru') <> '' AND jsonb_typeof(release_notes->'en') = 'string' AND btrim(release_notes->>'en') <> ''",
    "(delivery_method = 'direct_apk' AND artifact_storage_key IS NOT NULL AND artifact_sha256 IS NOT NULL AND artifact_size_bytes > 0) OR (delivery_method = 'eas_update' AND eas_update_group_id IS NOT NULL AND runtime_version IS NOT NULL AND artifact_storage_key IS NULL) OR (delivery_method = 'google_play' AND artifact_storage_key IS NULL)",
    "delivery_method = 'direct_apk' OR (artifact_sha256 IS NULL AND artifact_size_bytes IS NULL)",
    "status IN ('published', 'withdrawn')",
    "(status = 'published' AND withdrawn_at IS NULL AND withdrawal_reason IS NULL) OR (status = 'withdrawn' AND withdrawn_at IS NOT NULL AND withdrawal_reason IS NOT NULL)",
    "delivery_method = 'direct_apk'",
    "version_code DESC",
    "published_at DESC",
    "status = 'published'",
    "delivery_method = 'eas_update' OR eas_update_id IS NULL",
}


def _call_path(node: ast.expr) -> tuple[str, ...] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return tuple(reversed(parts))


def _targets_reserved_binding(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id in RESERVED_BINDINGS
        for child in ast.walk(node)
    )


def _validate_sql_fragment(node: ast.expr) -> None:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        raise ValueError("raw_sql_must_be_constant")
    value = node.value
    if not value.isascii() or len(value) > 4096 or value not in SAFE_SQL_FRAGMENTS:
        raise ValueError("raw_sql_not_reviewed")


def validate(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    upgrades = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"]
    if len(upgrades) != 1:
        raise ValueError("upgrade_function_invalid")
    upgrade = upgrades[0]
    alembic_imports = 0
    sqlalchemy_imports = 0
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            if statement.module == "alembic" and [(item.name, item.asname) for item in statement.names] == [("op", None)]:
                alembic_imports += 1
            elif statement.module == "sqlalchemy.dialects" and [(item.name, item.asname) for item in statement.names] == [("postgresql", None)]:
                pass
            elif statement.module not in {"typing", "__future__"}:
                raise ValueError("import_not_allowlisted")
        elif isinstance(statement, ast.Import):
            if [(item.name, item.asname) for item in statement.names] == [("sqlalchemy", "sa")]:
                sqlalchemy_imports += 1
            else:
                raise ValueError("import_not_allowlisted")
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not isinstance(statement, ast.FunctionDef) or statement.name not in {"upgrade", "downgrade"}:
                raise ValueError("helper_function_rejected")
            arguments = statement.args
            if (
                statement.decorator_list
                or arguments.posonlyargs
                or arguments.args
                or arguments.vararg is not None
                or arguments.kwonlyargs
                or arguments.kwarg is not None
                or arguments.defaults
                or any(value is not None for value in arguments.kw_defaults)
            ):
                raise ValueError("migration_function_signature_rejected")
            if statement.returns is not None and not (
                isinstance(statement.returns, ast.Constant) and statement.returns.value is None
            ):
                raise ValueError("migration_function_annotation_rejected")
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            if any(isinstance(node, ast.Call) for node in ast.walk(statement)):
                raise ValueError("module_scope_call_rejected")
            if any(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in RESERVED_BINDINGS for node in ast.walk(statement)):
                raise ValueError("canonical_binding_aliased")
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if any(_targets_reserved_binding(target) for target in targets):
                raise ValueError("canonical_binding_reassigned")
        elif not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str)):
            raise ValueError("module_statement_rejected")
    if alembic_imports != 1 or sqlalchemy_imports != 1:
        raise ValueError("canonical_import_missing")
    if not upgrade.body:
        raise ValueError("upgrade_body_empty")
    for statement in upgrade.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            raise ValueError("upgrade_statement_rejected")
        operation = _call_path(statement.value.func)
        if operation is None or len(operation) != 2 or operation[0] != "op" or operation[1] not in ALLOWED_OP_CALLS:
            raise ValueError("upgrade_operation_rejected")
    parents = {child: parent for parent in ast.walk(upgrade) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(upgrade):
        if node is not upgrade and isinstance(
            node,
            (
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.FunctionDef,
                ast.Global,
                ast.Import,
                ast.ImportFrom,
                ast.Lambda,
                ast.Nonlocal,
            ),
        ):
            raise ValueError("nested_scope_or_import_rejected")
        if isinstance(node, (ast.AsyncFor, ast.AsyncWith, ast.For, ast.If, ast.Match, ast.Try, ast.While, ast.With)):
            raise ValueError("control_flow_rejected")
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            raise ValueError("comprehension_rejected")
        if isinstance(node, ast.ExceptHandler) and node.name in RESERVED_BINDINGS:
            raise ValueError("canonical_binding_shadowed")
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in RESERVED_BINDINGS:
            raise ValueError("canonical_binding_reassigned")
        if isinstance(node, ast.arg) and node.arg in RESERVED_BINDINGS:
            raise ValueError("canonical_binding_shadowed")
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
            for target in targets:
                if target is not None and _targets_reserved_binding(target):
                    raise ValueError("canonical_binding_reassigned")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in {"op", "sa", "postgresql"}:
            expression: ast.AST = node
            parent = parents.get(expression)
            while isinstance(parent, ast.Attribute) and parent.value is expression:
                expression = parent
                parent = parents.get(expression)
            if not isinstance(parent, ast.Call) or parent.func is not expression:
                raise ValueError("canonical_binding_aliased")
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        path_parts = _call_path(node.func)
        if path_parts in {("sa", "text"), ("sqlalchemy", "text"), ("sa", "CheckConstraint"), ("sqlalchemy", "CheckConstraint")}:
            if not node.args:
                raise ValueError("raw_sql_missing")
            _validate_sql_fragment(node.args[0])
        if path_parts == ("op", "create_check_constraint"):
            if len(node.args) < 3:
                raise ValueError("raw_sql_missing")
            condition = node.args[2]
            if isinstance(condition, ast.Call) and _call_path(condition.func) in {("sa", "text"), ("sqlalchemy", "text")}:
                if not condition.args:
                    raise ValueError("raw_sql_missing")
                _validate_sql_fragment(condition.args[0])
            else:
                _validate_sql_fragment(condition)
        if path_parts and len(path_parts) == 2 and path_parts[0] == "op" and path_parts[1] in ALLOWED_OP_CALLS:
            continue
        if path_parts and path_parts[0] in CONSTRUCTOR_ROOTS and path_parts[-1] in ALLOWED_CONSTRUCTORS:
            continue
        raise ValueError("non_additive_call_rejected")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        validate(Path(argv[1]))
    except (OSError, SyntaxError, UnicodeError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
