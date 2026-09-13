#!/usr/bin/env python3
"""Fail closed unless Compose's effective API URL equals a protected env file."""

from __future__ import annotations

import hmac
import json
import os
from pathlib import PurePosixPath
import stat
import sys


def _protected_bytes(path: str) -> bytes:
    if not PurePosixPath(path).is_absolute() or path == "/" or any(ord(ch) < 33 or ord(ch) == 127 for ch in path):
        raise ValueError("protected_path_invalid")
    parts = PurePosixPath(path).parts[1:]
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
                os.close(child)
                raise ValueError("protected_parent_invalid")
            os.close(parent)
            parent = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) not in (0o400, 0o600, 0o640):
                raise ValueError("protected_file_invalid")
            payload = os.read(descriptor, 65537)
            if len(payload) > 65536:
                raise ValueError("protected_file_too_large")
            return payload
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def verify(config_bytes: bytes, expected_bytes: bytes) -> None:
    if len(config_bytes) > 1048576 or len(expected_bytes) > 65536:
        raise ValueError("compose_database_guard_input_invalid")
    try:
        config = json.loads(config_bytes)
        actual = config["services"]["api"]["environment"]["DATABASE_URL"]
        lines = expected_bytes.decode("utf-8").splitlines()
        expected_values = [line[len("DATABASE_URL="):] for line in lines if line.startswith("DATABASE_URL=")]
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise ValueError("compose_database_guard_input_invalid") from None
    if len(expected_values) != 1 or not isinstance(actual, str):
        raise ValueError("compose_database_guard_input_invalid")
    expected = expected_values[0]
    if not expected or not expected.isascii() or not actual.isascii() or any(ord(ch) < 33 or ord(ch) == 127 for ch in expected):
        raise ValueError("compose_database_guard_input_invalid")
    if not hmac.compare_digest(actual, expected):
        raise ValueError("compose_database_target_mismatch")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        verify(sys.stdin.buffer.read(1048577), _protected_bytes(argv[1]))
    except (OSError, ValueError):
        print("compose_database_guard=failed", file=sys.stderr)
        return 1
    print("compose_database_guard=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
