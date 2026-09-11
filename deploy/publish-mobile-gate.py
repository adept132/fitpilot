#!/usr/bin/env python3
"""Durably publish the backend-before-mobile gate exactly once."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("gate_write_incomplete")
        view = view[written:]


def publish(evidence: Path, gate: Path, backend: str, mobile: str, completed: str) -> None:
    with evidence.open("rb") as handle:
        evidence_payload = handle.read()
        os.fsync(handle.fileno())
    _fsync_directory(evidence.parent)
    _fsync_directory(evidence.parent.parent)
    evidence_sha256 = hashlib.sha256(evidence_payload).hexdigest()
    content = (
        f"backend_gate=passed\nbackend_sha={backend}\nmobile_candidate_sha={mobile}\n"
        f"deploy_completed_at={completed}\nevidence_sha256={evidence_sha256}\n"
        "write_mobile_gate_exit=0\n"
    ).encode("ascii")
    directory = gate.parent
    descriptor, temporary_name = tempfile.mkstemp(prefix=".backend-gate.", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, content)
        if os.fstat(descriptor).st_size != len(content):
            raise OSError("gate_write_incomplete")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, gate)
        _fsync_directory(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        _fsync_directory(directory)


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        return 2
    publish(Path(argv[1]), Path(argv[2]), argv[3], argv[4], argv[5])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
