#!/usr/bin/env python3
"""Snapshot the guarded restore DB URL and serialize a safe Compose overlay."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from urllib.parse import unquote, urlsplit


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short_write")
        offset += written


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise ValueError("database_url_file_too_large")
    return payload


def _open_protected_file(path: Path) -> int:
    value = str(path)
    if not PurePosixPath(value).is_absolute() or value == "/" or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("database_url_path_invalid")
    parts = PurePosixPath(value).parts[1:]
    parent_descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        root_metadata = os.fstat(parent_descriptor)
        if not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_uid != 0 or stat.S_IMODE(root_metadata.st_mode) & 0o022:
            raise ValueError("database_url_parent_untrusted")
        for part in parts[:-1]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
            metadata = os.fstat(next_descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
                os.close(next_descriptor)
                raise ValueError("database_url_parent_untrusted")
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        return os.open(
            parts[-1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
    finally:
        os.close(parent_descriptor)


def validate_database_url(payload: bytes) -> bytes:
    if not payload or len(payload) > 4096 or not payload.isascii():
        raise ValueError("database_url_file_invalid")
    lines = payload.decode("ascii").splitlines()
    if len(lines) != 1 or payload != (lines[0] + "\n").encode("ascii") or not lines[0].startswith("DATABASE_URL="):
        raise ValueError("database_url_file_invalid")
    value = lines[0].split("=", 1)[1]
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError("database_url_invalid")
    parsed = urlsplit(value)
    database = unquote(parsed.path.lstrip("/"))
    if parsed.scheme not in {"postgresql", "postgresql+asyncpg"}:
        raise ValueError("database_url_invalid")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.query or parsed.fragment:
        raise ValueError("restore_database_not_local")
    try:
        if parsed.port == 0:
            raise ValueError("database_port_invalid")
    except ValueError as error:
        raise ValueError("database_port_invalid") from error
    if re.fullmatch(r"eurith_restore_[a-z0-9][a-z0-9_]*", database) is None:
        raise ValueError("restore_database_name_invalid")
    return payload


def overlay_bytes(snapshot: str, probe: str) -> bytes:
    for value in (snapshot, probe):
        if not PurePosixPath(value).is_absolute() or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("overlay_path_invalid")
    quoted_snapshot = json.dumps(snapshot, ensure_ascii=True)
    quoted_probe = json.dumps(probe, ensure_ascii=True)
    return (
        "services:\n"
        "  api:\n"
        "    env_file:\n"
        f"      - {quoted_snapshot}\n"
        "    volumes:\n"
        "      - type: bind\n"
        f"        source: {quoted_probe}\n"
        "        target: /tmp/eurith-rehearse-release-db.py\n"
        "        read_only: true\n"
    ).encode("ascii")


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        _write_all(descriptor, payload)
        if os.fstat(descriptor).st_size != len(payload):
            raise OSError("snapshot_size_mismatch")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare(source: Path, probe: Path) -> tuple[Path, Path, Path]:
    descriptor = _open_protected_file(source)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o400 or metadata.st_uid != 0 or metadata.st_gid != 0:
            raise ValueError("database_url_file_permissions_invalid")
        payload = validate_database_url(_read_bounded(descriptor, 4096))
    finally:
        os.close(descriptor)
    if not probe.is_absolute() or not probe.is_file() or probe.is_symlink():
        raise ValueError("rehearsal_probe_invalid")
    directory = Path(tempfile.mkdtemp(prefix="eurith-release-rehearsal.", dir="/tmp"))
    snapshot = directory / "restore-db.env"
    overlay = directory / "compose.rehearsal.yml"
    try:
        os.chmod(directory, 0o700)
        directory_metadata = directory.stat()
        if stat.S_IMODE(directory_metadata.st_mode) != 0o700 or directory_metadata.st_uid != 0 or directory_metadata.st_gid != 0:
            raise ValueError("rehearsal_directory_permissions_invalid")
        _write_exclusive(snapshot, payload)
        _write_exclusive(overlay, overlay_bytes(str(snapshot), str(probe)))
        directory_descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        for child in (overlay, snapshot):
            try:
                child.unlink()
            except FileNotFoundError:
                pass
        directory.rmdir()
        raise
    return directory, snapshot, overlay


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 2
    try:
        directory, snapshot, overlay = prepare(Path(argv[1]), Path(argv[2]))
    except (OSError, UnicodeError, ValueError):
        return 1
    print(f"rehearsal_dir={directory}")
    print(f"rehearsal_database_url_snapshot={snapshot}")
    print(f"rehearsal_overlay={overlay}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
