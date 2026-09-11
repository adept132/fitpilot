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


def _call_path(node: ast.expr) -> tuple[str, ...] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return tuple(reversed(parts))


def validate(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    upgrades = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"]
    if len(upgrades) != 1:
        raise ValueError("upgrade_function_invalid")
    upgrade = upgrades[0]
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exposed = [*statement.decorator_list, *statement.args.defaults, *statement.args.kw_defaults]
            if any(isinstance(node, ast.Call) for value in exposed if value is not None for node in ast.walk(value)):
                raise ValueError("module_scope_call_rejected")
        elif any(isinstance(node, ast.Call) for node in ast.walk(statement)):
            raise ValueError("module_scope_call_rejected")
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        path_parts = _call_path(node.func)
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
