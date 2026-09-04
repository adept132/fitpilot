"""Dry-run-first cleanup for the Android release volume.

The registry is the authority for APK ownership.  Files are removed only from
the configured content-addressed layout and only while the cleanup lock is
held, which also serializes direct-APK finalization.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterable

from sqlalchemy import select, text

from api.services.models import AppRelease


STAGING_RETENTION = timedelta(days=1)
ORPHAN_RETENTION = timedelta(days=7)
WITHDRAWN_RETENTION = timedelta(days=30)
_DIGEST_LENGTH = 64
_APK_SUFFIX = ".apk"
_CLEANUP_LOCK_KEY = -613_777_772_007_134_079


class CleanupSafetyError(RuntimeError):
    """Raised instead of following an unsafe storage path."""


@dataclass(frozen=True)
class CleanupCandidate:
    action: str
    storage_key: str
    reason: str
    size_bytes: int
    path: Path
    release: Any | None = None


def cleanup_advisory_key() -> int:
    """Stable global lock shared by cleanup and direct-APK finalization."""
    return _CLEANUP_LOCK_KEY


async def acquire_release_cleanup_lock(session: Any) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": cleanup_advisory_key()},
    )


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("cleanup time must be timezone-aware")
    return current.astimezone(UTC)


def _is_symlink(path: Path) -> bool:
    return stat.S_ISLNK(os.lstat(path).st_mode)


def _require_directory(path: Path, *, optional: bool = False) -> Path | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if optional:
            return None
        raise CleanupSafetyError(f"storage directory is missing: {path}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CleanupSafetyError(f"storage directory is unsafe: {path}")
    return path


def _regular_file(path: Path) -> os.stat_result | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise CleanupSafetyError(f"release artifact is unsafe: {path}")
    return metadata


def _older_than(path: Path, cutoff: datetime) -> tuple[bool, int]:
    metadata = _regular_file(path)
    if metadata is None:
        return False, 0
    modified = datetime.fromtimestamp(metadata.st_mtime, tz=UTC)
    return modified < cutoff, metadata.st_size


def _safe_unlink(root: Path, candidate: CleanupCandidate) -> None:
    try:
        relative = candidate.path.relative_to(root)
    except ValueError as error:
        raise CleanupSafetyError("cleanup path escapes storage root") from error
    current = root
    _require_directory(current)
    for component in relative.parts[:-1]:
        current = current / component
        _require_directory(current)
    _regular_file(candidate.path)
    candidate.path.unlink()


def _artifact_key_is_valid(key: str) -> bool:
    prefix = "android/sha256/"
    if not key.startswith(prefix) or not key.endswith(_APK_SUFFIX):
        return False
    digest = key[len(prefix) : -len(_APK_SUFFIX)]
    return len(digest) == _DIGEST_LENGTH and all(char in "0123456789abcdef" for char in digest)


def _staging_candidates(root: Path, cutoff: datetime) -> list[CleanupCandidate]:
    staging = _require_directory(root / ".staging", optional=True)
    if staging is None:
        return []
    candidates: list[CleanupCandidate] = []
    with os.scandir(staging) as entries:
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                raise CleanupSafetyError(f"release artifact is unsafe: {path}")
            if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".apk.part"):
                continue
            old, size = _older_than(path, cutoff)
            if old:
                candidates.append(
                    CleanupCandidate("delete", f".staging/{entry.name}", "stale-staging", size, path)
                )
    return candidates


def _apk_files(root: Path) -> Iterable[tuple[str, Path]]:
    android = _require_directory(root / "android", optional=True)
    if android is None:
        return []
    sha256_directory = _require_directory(android / "sha256", optional=True)
    if sha256_directory is None:
        return []
    files: list[tuple[str, Path]] = []
    with os.scandir(sha256_directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                raise CleanupSafetyError(f"release artifact is unsafe: {path}")
            if not entry.is_file(follow_symlinks=False):
                continue
            key = f"android/sha256/{entry.name}"
            if _artifact_key_is_valid(key):
                files.append((key, path))
    return files


def _active_references(releases: Iterable[Any], storage_key: str) -> list[Any]:
    return [
        release
        for release in releases
        if getattr(release, "artifact_storage_key", None) == storage_key
        and getattr(release, "artifact_deleted_at", None) is None
    ]


def _eligible_withdrawn(release: Any, cutoff: datetime) -> bool:
    withdrawn_at = getattr(release, "withdrawn_at", None)
    if (
        getattr(release, "status", None) != "withdrawn"
        or bool(getattr(release, "is_mandatory", False))
        or withdrawn_at is None
    ):
        return False
    if withdrawn_at.tzinfo is None or withdrawn_at.utcoffset() is None:
        return False
    return withdrawn_at.astimezone(UTC) < cutoff


async def cleanup_release_storage(
    session: Any,
    root: Path,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> list[CleanupCandidate]:
    """Report candidates, and delete them only when explicitly requested.

    The caller owns the database transaction.  In apply mode the deletion and
    ``artifact_deleted_at`` update therefore commit or roll back together with
    surrounding registry work.
    """
    timestamp = _now(now)
    storage_root = Path(root).absolute()
    _require_directory(storage_root)
    await acquire_release_cleanup_lock(session)
    releases = (await session.execute(select(AppRelease))).scalars().all()

    staging = _staging_candidates(storage_root, timestamp - STAGING_RETENTION)
    apk_files = list(_apk_files(storage_root))
    withdrawn_cutoff = timestamp - WITHDRAWN_RETENTION
    orphan_cutoff = timestamp - ORPHAN_RETENTION
    artifacts: list[CleanupCandidate] = []

    for storage_key, path in apk_files:
        old_for_orphan, size = _older_than(path, orphan_cutoff)
        references = _active_references(releases, storage_key)
        if not references:
            if old_for_orphan:
                artifacts.append(
                    CleanupCandidate("delete", storage_key, "orphan-artifact", size, path)
                )
            continue
        if len(references) != 1:
            continue
        release = references[0]
        old_for_withdrawn, _ = _older_than(path, withdrawn_cutoff)
        if old_for_withdrawn and _eligible_withdrawn(release, withdrawn_cutoff):
            artifacts.append(
                CleanupCandidate("delete", storage_key, "withdrawn-artifact", size, path, release)
            )

    candidates = sorted(staging + artifacts, key=lambda item: item.storage_key)
    for candidate in candidates:
        print(
            json.dumps(
                {
                    "action": candidate.action,
                    "storage_key": candidate.storage_key,
                    "reason": candidate.reason,
                    "size_bytes": candidate.size_bytes,
                },
                separators=(",", ":"),
            )
        )
    if apply:
        for candidate in candidates:
            _safe_unlink(storage_root, candidate)
            if candidate.release is not None:
                candidate.release.artifact_deleted_at = timestamp
                await session.flush()
    return candidates


async def _run(args: argparse.Namespace) -> int:
    from app.database import SessionLocal

    root = Path(os.environ.get("RELEASE_STORAGE_ROOT", "/var/lib/eurith/releases"))
    async with SessionLocal() as session:
        async with session.begin():
            await cleanup_release_storage(session, root, apply=args.apply)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean stale Android release artifacts")
    parser.add_argument("--apply", action="store_true", help="delete eligible artifacts")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
