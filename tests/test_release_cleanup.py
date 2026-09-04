from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts.cleanup_app_releases import CleanupSafetyError, cleanup_release_storage


NOW = datetime(2026, 9, 5, tzinfo=UTC)
OLD = NOW - timedelta(days=31)


class _Scalars:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return list(self._values)


class _Result:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> _Scalars:
        return _Scalars(self._values)


class CleanupSession:
    """Small DB boundary fake; filesystem effects stay real in tmp_path."""

    def __init__(self, releases: list[object]) -> None:
        self.releases = releases
        self.advisory_locks: list[object] = []
        self.flushes = 0

    async def execute(self, statement, params=None):
        if "pg_advisory_xact_lock" in str(statement):
            self.advisory_locks.append(params["key"])
            return _Result([])
        return _Result(self.releases)

    async def flush(self) -> None:
        self.flushes += 1


def digest(value: str) -> str:
    return value * 64


def artifact(root: Path, value: str, contents: bytes = b"APK") -> tuple[str, Path]:
    sha = digest(value)
    key = f"android/sha256/{sha}.apk"
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    return key, path


def release(
    storage_key: str,
    *,
    status: str = "withdrawn",
    withdrawn_at: datetime | None = OLD,
    mandatory: bool = False,
    deleted_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        storage_key=storage_key,
        artifact_storage_key=storage_key,
        artifact_deleted_at=deleted_at,
        status=status,
        withdrawn_at=withdrawn_at,
        is_mandatory=mandatory,
    )


@pytest.mark.asyncio
async def test_cleanup_dry_run_reports_eligible_files_without_mutating_disk_or_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches an accidental deletion or DB marker update when --apply is absent."""
    key, path = artifact(tmp_path, "a")
    row = release(key)
    session = CleanupSession([row])

    candidates = await cleanup_release_storage(session, tmp_path, now=NOW)

    assert [(item.action, item.storage_key, item.reason, item.size_bytes) for item in candidates] == [
        ("delete", key, "withdrawn-artifact", 3)
    ]
    assert path.read_bytes() == b"APK"
    assert row.artifact_deleted_at is None
    assert session.flushes == 0
    assert len(session.advisory_locks) == 1
    assert capsys.readouterr().out == (
        '{"action":"delete","storage_key":"android/sha256/' + digest("a")
        + '.apk","reason":"withdrawn-artifact","size_bytes":3}\n'
    )


@pytest.mark.asyncio
async def test_cleanup_apply_deletes_old_staging_and_orphan_files_but_not_fresh_files(
    tmp_path: Path,
) -> None:
    """Catches cleanup that leaves eligible trash or removes files younger than the cutoff."""
    staging = tmp_path / ".staging"
    staging.mkdir()
    old_part = staging / "old.apk.part"
    fresh_part = staging / "fresh.apk.part"
    old_part.write_bytes(b"old")
    fresh_part.write_bytes(b"fresh")
    os.utime(old_part, (OLD.timestamp(), OLD.timestamp()))
    orphan_key, old_orphan = artifact(tmp_path, "b")
    fresh_orphan_key, fresh_orphan = artifact(tmp_path, "c")
    fresh_time = NOW - timedelta(hours=1)
    os.utime(fresh_orphan, (fresh_time.timestamp(), fresh_time.timestamp()))
    session = CleanupSession([])

    candidates = await cleanup_release_storage(session, tmp_path, apply=True, now=NOW)

    assert {(item.storage_key, item.reason) for item in candidates} == {
        (".staging/old.apk.part", "stale-staging"),
        (orphan_key, "orphan-artifact"),
    }
    assert not old_part.exists()
    assert not old_orphan.exists()
    assert fresh_part.exists()
    assert fresh_orphan.exists()
    assert fresh_orphan_key not in {item.storage_key for item in candidates}


@pytest.mark.asyncio
async def test_cleanup_retains_shared_published_and_mandatory_artifacts(
    tmp_path: Path,
) -> None:
    """Catches deletion of a digest still referenced by another row or protected release."""
    shared_key, shared_path = artifact(tmp_path, "d")
    mandatory_key, mandatory_path = artifact(tmp_path, "e")
    session = CleanupSession([
        release(shared_key),
        release(shared_key, status="published", withdrawn_at=None),
        release(mandatory_key, mandatory=True),
    ])

    candidates = await cleanup_release_storage(session, tmp_path, apply=True, now=NOW)

    assert candidates == []
    assert shared_path.exists()
    assert mandatory_path.exists()
    assert all(row.artifact_deleted_at is None for row in session.releases)


@pytest.mark.asyncio
async def test_cleanup_apply_marks_only_the_deleted_old_withdrawn_release(
    tmp_path: Path,
) -> None:
    """Catches loss of withdrawal audit metadata after a safe artifact removal."""
    key, path = artifact(tmp_path, "f")
    row = release(key)
    session = CleanupSession([row])

    await cleanup_release_storage(session, tmp_path, apply=True, now=NOW)

    assert not path.exists()
    assert row.artifact_deleted_at == NOW
    assert session.flushes == 1


@pytest.mark.asyncio
async def test_cleanup_rejects_symlinked_staging_without_touching_target(
    tmp_path: Path,
) -> None:
    """Catches a path traversal that would follow a staging symlink outside release storage."""
    outside = tmp_path.parent / "outside-releases"
    outside.mkdir()
    protected = outside / "protected.apk.part"
    protected.write_bytes(b"keep")
    try:
        (tmp_path / ".staging").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this runner")

    with pytest.raises(CleanupSafetyError, match="symlink|unsafe"):
        await cleanup_release_storage(CleanupSession([]), tmp_path, apply=True, now=NOW)

    assert protected.read_bytes() == b"keep"
