"""Fail-closed real-PostgreSQL tests for release artifact cleanup."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
import hashlib, os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest, pytest_asyncio
from fastapi import UploadFile
from sqlalchemy import delete, text

from app.database import SessionLocal, engine
from api.routers import internal_releases
from api.services.models import AppRelease, AppReleaseLane
from api.services.release_registry import set_expected_commit
from api.services.release_storage import ReleaseStorage
from scripts import cleanup_app_releases

NOW = datetime(2026, 9, 5, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def disposable_cleanup_database() -> list[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    parsed = urlparse((url or "").replace("postgresql+asyncpg", "postgresql", 1))
    if not url or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or not parsed.path.lstrip("/").startswith("fitpilot_task7_"):
        pytest.fail("TEST_DATABASE_URL must name a local fitpilot_task7_* disposable database")
    commits: list[str] = []
    async with SessionLocal() as session:
        existing_lane = await session.get(
            AppReleaseLane, {"platform": "android", "channel": "production-direct"}
        )
        lane_snapshot = (
            (existing_lane.expected_source_commit, existing_lane.expected_ci_run_id)
            if existing_lane is not None
            else None
        )
    try:
        yield commits
    finally:
        if commits:
            async with SessionLocal() as session:
                await session.execute(delete(AppRelease).where(AppRelease.source_commit.in_(commits)))
                lane = await session.get(
                    AppReleaseLane, {"platform": "android", "channel": "production-direct"}
                )
                if lane_snapshot is None and lane is not None:
                    await session.delete(lane)
                elif lane_snapshot is not None:
                    if lane is None:
                        session.add(AppReleaseLane(platform="android", channel="production-direct", expected_source_commit=lane_snapshot[0], expected_ci_run_id=lane_snapshot[1]))
                    else:
                        lane.expected_source_commit, lane.expected_ci_run_id = lane_snapshot
                await session.commit()


def identity() -> tuple[str, str]:
    marker = uuid4().hex
    return hashlib.sha1(marker.encode()).hexdigest(), marker


def artifact(root: Path, marker: str) -> tuple[str, str, Path]:
    payload = b"PK\x03\x04" + marker.encode(); digest = hashlib.sha256(payload).hexdigest()
    key = f"android/sha256/{digest}.apk"; path = root / key; path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload); old = NOW - timedelta(days=31); os.utime(path, (old.timestamp(), old.timestamp()))
    return key, digest, path


def withdrawn(key: str, digest: str, size: int, source: str, marker: str) -> AppRelease:
    return AppRelease(id=uuid4(), platform="android", channel="production-direct", delivery_method="direct_apk", version_code=1, version_name="1.0.0", runtime_version=None, fingerprint=None, release_notes={"ru":"Исправление","en":"Fix"}, status="withdrawn", is_mandatory=False, min_supported_version_code=None, artifact_storage_key=key, artifact_sha256=digest, artifact_size_bytes=size, source_commit=source, ci_run_id=f"cleanup-{marker}", idempotency_key=f"cleanup-{marker}", eas_build_id=None, eas_update_group_id=None, withdrawn_at=NOW-timedelta(days=31), withdrawal_reason="fixture")


async def add_withdrawn(db, root: Path, commits: list[str]) -> tuple[AppRelease, Path]:
    source, marker = identity(); commits.append(source); key, digest, path = artifact(root, marker)
    row = withdrawn(key, digest, path.stat().st_size, source, marker); db.add(row); await db.commit(); return row, path


@pytest.mark.asyncio
async def test_cleanup_recovers_after_commit_before_unlink_with_real_postgresql(db, tmp_path, monkeypatch, disposable_cleanup_database):
    root = tmp_path / "release-volume"; row, path = await add_withdrawn(db, root, disposable_cleanup_database)
    monkeypatch.setattr(cleanup_app_releases, "apply_cleanup_candidates", lambda *_: (_ for _ in ()).throw(RuntimeError("crash after marker")))
    with pytest.raises(RuntimeError, match="crash after marker"):
        await cleanup_app_releases.cleanup_release_volume(engine, root, apply=True, now=NOW)
    async with SessionLocal() as session:
        assert (await session.get(AppRelease, row.id)).artifact_deleted_at == NOW
    assert path.exists(); monkeypatch.undo()
    await cleanup_app_releases.cleanup_release_volume(engine, root, apply=True, now=NOW); assert not path.exists()


@pytest.mark.asyncio
async def test_cleanup_flush_failure_rolls_back_marker_without_unlink(db, tmp_path, monkeypatch, disposable_cleanup_database):
    root = tmp_path / "release-volume"; row, path = await add_withdrawn(db, root, disposable_cleanup_database)
    async def fail_flush(*args, **kwargs): raise RuntimeError("marker flush failure")
    monkeypatch.setattr(cleanup_app_releases, "mark_cleanup_deletions", fail_flush)
    with pytest.raises(RuntimeError, match="flush failure"):
        await cleanup_app_releases.cleanup_release_volume(engine, root, apply=True, now=NOW)
    async with SessionLocal() as session:
        assert (await session.get(AppRelease, row.id)).artifact_deleted_at is None
    assert path.exists()


@pytest.mark.asyncio
async def test_cleanup_commit_failure_rolls_back_marker_without_unlink(db, tmp_path, monkeypatch, disposable_cleanup_database):
    root = tmp_path / "release-volume"; row, path = await add_withdrawn(db, root, disposable_cleanup_database); original = cleanup_app_releases.mark_cleanup_deletions
    async def break_commit(session, candidates, *, now):
        await original(session, candidates, now=now); await session.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
    monkeypatch.setattr(cleanup_app_releases, "mark_cleanup_deletions", break_commit)
    with pytest.raises(Exception):
        await cleanup_app_releases.cleanup_release_volume(engine, root, apply=True, now=NOW)
    async with SessionLocal() as session:
        assert (await session.get(AppRelease, row.id)).artifact_deleted_at is None
    assert path.exists()


@pytest.mark.asyncio
async def test_cleanup_blocks_real_direct_apk_route(tmp_path, monkeypatch, disposable_cleanup_database):
    source, marker = identity(); disposable_cleanup_database.append(source); root = tmp_path / "release-volume"; artifact(root, marker)
    cleaner_entered, release_cleaner, publisher_waiting, publisher_acquired = (asyncio.Event() for _ in range(4)); plan = cleanup_app_releases.cleanup_release_storage; route_lock = internal_releases.hold_release_artifact_lock
    async def pause(*args, **kwargs):
        result = await plan(*args, **kwargs); cleaner_entered.set(); await release_cleaner.wait(); return result
    @asynccontextmanager
    async def observe(bound_engine):
        publisher_waiting.set()
        async with route_lock(bound_engine) as connection:
            publisher_acquired.set(); yield connection
    monkeypatch.setattr(cleanup_app_releases, "cleanup_release_storage", pause); monkeypatch.setattr(internal_releases, "hold_release_artifact_lock", observe)
    monkeypatch.setattr(internal_releases, "_storage", lambda: ReleaseStorage(root))
    cleaner = asyncio.create_task(cleanup_app_releases.cleanup_release_volume(engine, root, now=NOW)); await asyncio.wait_for(cleaner_entered.wait(), timeout=5)
    async def publish():
        payload = b"PK\x03\x04route-publication"
        async with SessionLocal() as setup:
            async with setup.begin(): await set_expected_commit(setup, "android", "production-direct", source, f"route-{marker}")
        async with SessionLocal() as request_db:
            return await internal_releases.publish_direct_apk(channel="production-direct", source_commit=source, ci_run_id=f"route-{marker}", version_code=2, version_name="1.0.1", release_notes_ru="Исправление", release_notes_en="Fix", artifact=UploadFile(file=BytesIO(payload), filename="route.apk"), idempotency_key=f"route-{marker}", artifact_sha256=hashlib.sha256(payload).hexdigest(), db=request_db)
    publisher = asyncio.create_task(publish())
    try:
        await asyncio.wait_for(publisher_waiting.wait(), timeout=5); await asyncio.sleep(.1); assert not publisher_acquired.is_set() and not publisher.done()
        release_cleaner.set(); await asyncio.wait_for(cleaner, timeout=5); response = await asyncio.wait_for(publisher, timeout=5); assert publisher_acquired.is_set() and getattr(response, "status_code", 201) == 201
    finally:
        release_cleaner.set()
        for task in (cleaner, publisher):
            if not task.done(): task.cancel()
        await asyncio.gather(cleaner, publisher, return_exceptions=True)
