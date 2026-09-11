#!/usr/bin/env python3
"""Emit a deterministic identity for the exact Alembic upgrade path."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import re
import sys


REVISION = re.compile(r"^[0-9A-Za-z_]+$")


def _metadata(path: Path) -> tuple[str, str | None, bytes]:
    values: dict[str, object] = {}
    raw = path.read_bytes()
    if len(raw) > 1_048_576:
        raise ValueError("migration_file_too_large")
    tree = ast.parse(raw.decode("utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                values[target.id] = ast.literal_eval(node.value)
    revision, parent = values.get("revision"), values.get("down_revision")
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ValueError("revision_invalid")
    if parent is not None and (not isinstance(parent, str) or not REVISION.fullmatch(parent)):
        raise ValueError("down_revision_invalid")
    return revision, parent, raw


def build(versions: Path, old_head: str) -> tuple[str, str, str, list[str]]:
    if not versions.is_dir() or versions.is_symlink() or not REVISION.fullmatch(old_head):
        raise ValueError("input_invalid")
    revisions: dict[str, tuple[str | None, Path, bytes]] = {}
    for path in sorted(versions.glob("*.py"), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise ValueError("migration_file_invalid")
        revision, parent, raw = _metadata(path)
        if revision in revisions:
            raise ValueError("revision_duplicate")
        revisions[revision] = (parent, path, raw)
    parents = {parent for parent, _path, _raw in revisions.values() if parent is not None}
    heads = sorted(set(revisions) - parents)
    if len(heads) != 1 or old_head not in revisions:
        raise ValueError("migration_graph_invalid")
    target_head = heads[0]
    reverse_path: list[tuple[str, Path, bytes]] = []
    cursor = target_head
    seen: set[str] = set()
    while cursor != old_head:
        if cursor in seen or cursor not in revisions:
            raise ValueError("migration_path_invalid")
        seen.add(cursor)
        parent, path, raw = revisions[cursor]
        if parent is None:
            raise ValueError("migration_path_invalid")
        reverse_path.append((cursor, path, raw))
        cursor = parent
    entries: list[str] = []
    for revision, path, raw in reversed(reverse_path):
        digest = hashlib.sha256(raw).hexdigest()
        entries.append(f"{revision}:{path.name}:{digest}")
    manifest = "".join(f"{entry}\n" for entry in entries).encode("ascii")
    return target_head, f"{old_head}->{target_head}", hashlib.sha256(manifest).hexdigest(), entries


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 2
    try:
        target, path, digest, entries = build(Path(argv[1]), argv[2])
    except (OSError, SyntaxError, UnicodeError, ValueError):
        return 1
    print(f"old_head={argv[2]}")
    print(f"target_head={target}")
    print(f"migration_path={path}")
    print(f"migration_path_sha256={digest}")
    for entry in entries:
        print(f"migration_file={entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
