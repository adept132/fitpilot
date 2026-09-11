#!/usr/bin/env python3
"""Validate the protected, exact migration release approval."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import sys


IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{2,127}$")
REVISION = re.compile(r"^[0-9A-Za-z_]+$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_payload(metadata: os.stat_result, raw: bytes, old_sha: str, target_sha: str, rollback_sha: str, old_head: str, target_head: str, path_hash: str) -> tuple[str, str]:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("approval_not_regular")
    if stat.S_IMODE(metadata.st_mode) != 0o400 or metadata.st_uid != 0 or metadata.st_gid != 0:
        raise ValueError("approval_permissions_invalid")
    if not SHA40.fullmatch(old_sha) or not SHA40.fullmatch(target_sha) or not SHA40.fullmatch(rollback_sha):
        raise ValueError("backend_sha_invalid")
    if not REVISION.fullmatch(old_head) or not REVISION.fullmatch(target_head) or not SHA256.fullmatch(path_hash):
        raise ValueError("migration_identity_invalid")
    if len(raw) > 4096 or not raw.isascii():
        raise ValueError("approval_encoding_invalid")
    lines = raw.decode("ascii").splitlines()
    if len(lines) != 8 or raw != ("\n".join(lines) + "\n").encode("ascii"):
        raise ValueError("approval_format_invalid")
    prefix = [
        f"old_backend_sha={old_sha}",
        f"target_backend_sha={target_sha}",
        f"rollback_backend_sha={rollback_sha}",
        f"old_alembic_head={old_head}",
        f"target_alembic_head={target_head}",
        f"migration_path_sha256={path_hash}",
        "rollback_compatible=true",
    ]
    if lines[:7] != prefix or not lines[7].startswith("approval_identity="):
        raise ValueError("approval_mismatch")
    identity = lines[7].split("=", 1)[1]
    if not IDENTITY.fullmatch(identity):
        raise ValueError("approval_identity_invalid")
    return hashlib.sha256(raw).hexdigest(), identity


def validate(path: Path, old_sha: str, target_sha: str, rollback_sha: str, old_head: str, target_head: str, path_hash: str) -> tuple[str, str]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = 4097
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    return validate_payload(metadata, raw, old_sha, target_sha, rollback_sha, old_head, target_head, path_hash)


def main(argv: list[str]) -> int:
    if len(argv) != 8:
        return 2
    try:
        digest, identity = validate(Path(argv[1]), *argv[2:])
    except (OSError, UnicodeError, ValueError):
        return 1
    print(f"approval_sha256={digest}")
    print(f"approval_identity={identity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
