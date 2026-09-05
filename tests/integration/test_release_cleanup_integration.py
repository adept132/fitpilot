"""Real PostgreSQL checks for the cleanup commit/unlink and advisory-lock boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal, engine
from api.services.models import AppRelease
from api.services.release_artifact_lock import hold_release_artifact_lock
from scripts import cleanup_app_releases


NOW = datetime(2026, 9, 5, tzinfo=UTC)


def _artifact(root: Path) -> tuple[str, str, Path]:
    payload = b"PK\x03\x04cleanup-integration"
    digest = hashlib.sha256(payload).hexdigest()
    key = f"android/sha256/{digest}.apk"
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    old = NOW - timedelta(days=31)
    os.utime(path, (old.timestamp(), old.timestamp()))
    return key, digest, path


def _withdrawn_release(key: str, digest: str, size: int) -> AppRelease:
    return AppRelease(
        id=uuid4(),
        platform="android",
        channel="production-direct",
        delivery_method="direct_apk",
        version_code=1,
        version_name="1.0.0",
        runtime_version=None,
        fingerprint=None,
        release_notes={"ru": "Исправление", "en": "Fix"},
        status="withdrawn",
        is_mandatory=False,
        min_supported_version_code=None,
        artifact_storage_key=key,
        artifact_sha256=digest,
        artifact_size_bytes=size,
        source_commit="a" * 40,
        ci_run_id="cleanup-integration",
        idempotency_key=f"cleanup-{uuid4().hex}",
        eas_build_id=None,
        eas_update_group_id=None,
        withdrawn_at=NOW - timedelta(days=31),
        withdrawal_reason="integration fixture",
    )


@pytest.mark.asyncio
async def test_cleanup_recovers_after_commit_before_unlink_with_real_postgresql(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches missing artifact + unmarked row when the process dies after its commit."""
    root = tmp_path / "release-volume"
    key, digest, path = _artifact(root)
    row = _withdrawn_release(key, digest, path.stat().st_size)
    db.add(row)
    await db.commit()

    original_apply = cleanup_app_releases.apply_cleanup_candidates

    def crash_after_marker(*args, **kwargs) -> None:
        raise RuntimeError("simulated crash after marker commit")

    monkeypatch.setattr(cleanup_app_releases, "apply_cleanup_candidates", crash_after_marker)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await cleanup_app_releases.cleanup_release_volume(engine, root, apply=True, now=NOW)

    async with SessionLocal() as session:
        marked = await session.get(AppRelease, row.id)
        assert marked is not None
        assert marked.artifact_deleted_at == NOW
    assert path.exists()

    monkeypatch.setattr(cleanup_app_releases, "apply_cleanup_candidates", original_apply)
    await cleanup_app_releases.cleanup_release_volume(engine, root, apply=True, now=NOW)

    assert not path.exists()
    async with SessionLocal() as session:
        reconciled = await session.get(AppRelease, row.id)
        assert reconciled is not None
        assert reconciled.artifact_deleted_at == NOW


@pytest.mark.asyncio
async def test_cleanup_and_direct_publication_share_a_real_session_advisory_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches cleanup and direct publishing taking different PostgreSQL locks."""
    root = tmp_path / "release-volume"
    key, _digest, path = _artifact(root)
    # No registry row: the cleaner will plan an orphan while holding the lock.
    cleaner_entered = asyncio.Event()
    release_cleaner = asyncio.Event()
    publisher_entered = asyncio.Event()
    original_plan = cleanup_app_releases.cleanup_release_storage

    async def pause_cleaner(*args, **kwargs):
        result = await original_plan(*args, **kwargs)
        cleaner_entered.set()
        await release_cleaner.wait()
        return result

    monkeypatch.setattr(cleanup_app_releases, "cleanup_release_storage", pause_cleaner)

    cleaner = asyncio.create_task(
        cleanup_app_releases.cleanup_release_volume(engine, root, apply=False, now=NOW)
    )
    await cleaner_entered.wait()

    async def publish_critical_section() -> None:
        async with hold_release_artifact_lock(engine):
            publisher_entered.set()

    publisher = asyncio.create_task(publish_critical_section())
    await asyncio.sleep(0.1)
    assert not publisher_entered.is_set()
    assert path.exists()

    release_cleaner.set()
    await cleaner
    await publisher
    assert publisher_entered.is_set()
    assert key.endswith(".apk")
