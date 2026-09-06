"""End-to-end contracts for the protected Android release publication API.

These tests intentionally use the disposable PostgreSQL URL supplied to the
integration suite and a per-test artifact directory.  They never use the
deployment release volume.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from api.services.models import AppRelease, AppReleaseLane
from api.services import release_registry
from api.services.release_registry import advance_github_mobile_push_targets
from app.database import SessionLocal


APK_BYTES = b"PK\x03\x04" + b"release-api-fixture"
PUBLISHER_TOKEN = "publisher-token-for-integration-tests"
OPERATOR_TOKEN = "operator-token-for-integration-tests"
WEBHOOK_SECRET = "webhook-secret-for-integration-tests"


def _sha256(payload: bytes = APK_BYTES) -> str:
    return hashlib.sha256(payload).hexdigest()


def _commit(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def _metadata(
    *,
    source_commit: str,
    ci_run_id: str,
    version_code: int,
    min_supported_version_code: int | None = None,
) -> dict[str, str]:
    metadata = {
        "channel": "production-direct",
        "source_commit": source_commit,
        "ci_run_id": ci_run_id,
        "version_code": str(version_code),
        "version_name": "1.0.0",
        "runtime_version": "1.0.0",
        "fingerprint": "fingerprint-1",
        "release_notes_ru": "Исправления",
        "release_notes_en": "Fixes",
    }
    if min_supported_version_code is not None:
        metadata["min_supported_version_code"] = str(min_supported_version_code)
    return metadata


async def _signed_push(
    client, *, source_commit: str, delivery_id: str, before: str = "0" * 40, **changes
):
    event = changes.pop("event", "push")
    payload = {
        "repository": {"full_name": "adept132/eurith-mobile"},
        "ref": "refs/heads/main",
        "before": before,
        "after": source_commit,
        "deleted": False,
        **changes,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return await client.post(
        "/webhooks/github/mobile-push",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": signature,
        },
    )


async def _bind_ci_run(client, release_headers, *, source_commit: str, ci_run_id: str):
    return await client.post(
        "/internal/app-releases/lanes/android/production-direct/ci-run",
        headers=release_headers,
        json={"source_commit": source_commit, "ci_run_id": ci_run_id},
    )


@pytest_asyncio.fixture(autouse=True)
async def _release_test_state(db, tmp_path, monkeypatch):
    """Make every case independent without touching the deployment volume."""

    monkeypatch.setenv("RELEASE_PUBLISHER_TOKEN", PUBLISHER_TOKEN)
    monkeypatch.setenv("RELEASE_OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("RELEASE_STORAGE_ROOT", str(tmp_path / "releases"))
    await db.execute(delete(AppRelease))
    await db.execute(delete(AppReleaseLane))
    await db.commit()
    yield
    await db.execute(delete(AppRelease))
    await db.execute(delete(AppReleaseLane))
    await db.commit()


@pytest.fixture
def release_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {PUBLISHER_TOKEN}"}


@pytest.mark.asyncio
async def test_idempotency_lookup_finds_exact_withdrawn_nonlatest_and_cross_lane_rows(
    client, db, release_headers
):
    withdrawn = AppRelease(
        platform="android",
        channel="production-direct",
        delivery_method="direct_apk",
        version_code=7,
        version_name="1.0.7",
        runtime_version="1.0.7",
        fingerprint="fingerprint-direct-7",
        release_notes={"ru": "Старая версия", "en": "Old release"},
        status="withdrawn",
        withdrawn_at=datetime.now(UTC),
        withdrawal_reason="superseded test fixture",
        is_mandatory=False,
        min_supported_version_code=6,
        artifact_storage_key="android/sha256/" + "7" * 64 + ".apk",
        artifact_sha256="7" * 64,
        artifact_size_bytes=700,
        source_commit="7" * 40,
        ci_run_id="ci-direct-7",
        idempotency_key="direct:exact-withdrawn-7",
        eas_build_id="build-direct-7",
        eas_update_group_id=None,
    )
    newer = AppRelease(
        platform="android",
        channel="production-direct",
        delivery_method="direct_apk",
        version_code=8,
        version_name="1.0.8",
        runtime_version="1.0.8",
        fingerprint="fingerprint-direct-8",
        release_notes={"ru": "Новая версия", "en": "New release"},
        status="published",
        is_mandatory=False,
        min_supported_version_code=None,
        artifact_storage_key="android/sha256/" + "8" * 64 + ".apk",
        artifact_sha256="8" * 64,
        artifact_size_bytes=800,
        source_commit="8" * 40,
        ci_run_id="ci-direct-8",
        idempotency_key="direct:newer-8",
        eas_build_id="build-direct-8",
        eas_update_group_id=None,
    )
    play = AppRelease(
        platform="android",
        channel="production-play",
        delivery_method="eas_update",
        version_code=9,
        version_name="1.0.9",
        runtime_version="1.0.9",
        fingerprint="fingerprint-play-9",
        release_notes={"ru": "OTA версия", "en": "OTA release"},
        status="published",
        is_mandatory=True,
        min_supported_version_code=8,
        artifact_storage_key=None,
        artifact_sha256=None,
        artifact_size_bytes=None,
        source_commit="9" * 40,
        ci_run_id="ci-play-9",
        idempotency_key="eas:cross-lane-9",
        eas_build_id="build-play-9",
        eas_update_id="update-play-9",
        eas_update_group_id="update-group-play-9",
    )
    db.add_all([withdrawn, newer, play])
    await db.commit()

    old_response = await client.get(
        "/internal/app-releases/by-idempotency",
        params={"key": "direct:exact-withdrawn-7"},
        headers=release_headers,
    )
    play_response = await client.get(
        "/internal/app-releases/by-idempotency",
        params={"key": "eas:cross-lane-9"},
        headers=release_headers,
    )
    case_mismatch = await client.get(
        "/internal/app-releases/by-idempotency",
        params={"key": "DIRECT:exact-withdrawn-7"},
        headers=release_headers,
    )

    assert old_response.status_code == 200
    assert old_response.json()["release"] == {
        "id": str(withdrawn.id),
        "platform": "android",
        "channel": "production-direct",
        "delivery_method": "direct_apk",
        "source_commit": "7" * 40,
        "ci_run_id": "ci-direct-7",
        "idempotency_key": "direct:exact-withdrawn-7",
        "fingerprint": "fingerprint-direct-7",
        "runtime_version": "1.0.7",
        "version_code": 7,
        "version_name": "1.0.7",
        "eas_build_id": "build-direct-7",
        "eas_update_id": None,
        "eas_update_group_id": None,
        "status": "withdrawn",
        "is_mandatory": False,
        "min_supported_version_code": 6,
        "artifact_sha256": "7" * 64,
        "artifact_size_bytes": 700,
        "release_notes": {"ru": "Старая версия", "en": "Old release"},
    }
    assert play_response.status_code == 200
    assert play_response.json()["release"]["channel"] == "production-play"
    assert play_response.json()["release"]["eas_update_id"] == "update-play-9"
    assert play_response.json()["release"]["eas_update_group_id"] == "update-group-play-9"
    assert play_response.json()["release"]["artifact_sha256"] is None
    assert play_response.json()["release"]["release_notes"] == {
        "ru": "OTA версия",
        "en": "OTA release",
    }
    assert old_response.json()["release"]["release_notes"] != newer.release_notes
    assert "artifact_storage_key" not in old_response.text
    assert case_mismatch.status_code == 404


@pytest.mark.asyncio
async def test_publish_latest_download_withdraw_cycle(client, release_headers, tmp_path):
    source_commit = _commit("publish-cycle")
    delivery_id = "delivery-publish-cycle"
    version_code = 101

    target = await _signed_push(
        client, source_commit=source_commit, delivery_id=delivery_id
    )
    assert target.status_code == 200
    assert (
        await _bind_ci_run(
            client, release_headers, source_commit=source_commit, ci_run_id=delivery_id
        )
    ).status_code == 200

    publish = await client.post(
        "/internal/app-releases/android/direct-apk",
        headers={
            **release_headers,
            "Idempotency-Key": "direct:publish-cycle",
            "X-Artifact-SHA256": _sha256(),
        },
        data=_metadata(
            source_commit=source_commit,
            ci_run_id=delivery_id,
            version_code=version_code,
            min_supported_version_code=version_code - 1,
        ),
        files={"artifact": ("eurith.apk", APK_BYTES, "application/vnd.android.package-archive")},
    )
    assert publish.status_code == 201
    release_id = publish.json()["release"]["id"]

    latest = await client.get(
        "/app-releases/android/latest",
        params={
            "channel": "production-direct",
            "current_version_code": version_code - 2,
            "runtime_version": "1.0.0",
        },
    )
    assert latest.status_code == 200
    assert latest.json()["release"]["id"] == release_id
    assert latest.json()["release"]["min_supported_version_code"] == version_code - 1
    assert latest.json()["mandatory"] is True

    at_threshold = await client.get(
        "/app-releases/android/latest",
        params={
            "channel": "production-direct",
            "current_version_code": version_code - 1,
            "runtime_version": "1.0.0",
        },
    )
    assert at_threshold.status_code == 200
    assert at_threshold.json()["update_available"] is True
    assert at_threshold.json()["mandatory"] is False

    no_downgrade = await client.get(
        "/app-releases/android/latest",
        params={
            "channel": "production-direct",
            "current_version_code": version_code,
            "runtime_version": "1.0.0",
        },
    )
    assert no_downgrade.status_code == 200
    assert no_downgrade.json()["update_available"] is False
    assert no_downgrade.json()["mandatory"] is False
    assert no_downgrade.json()["release"] is None
    ledger = await client.get(
        "/internal/app-releases/lanes/android/production-direct", headers=release_headers
    )
    assert ledger.json() == {
        "platform": "android",
        "channel": "production-direct",
        "expected_source_commit": source_commit,
        "expected_ci_run_id": delivery_id,
        "latest_published": {
            "delivery_method": "direct_apk",
            "version_code": version_code,
            "version_name": "1.0.0",
            "fingerprint": "fingerprint-1",
            "runtime_version": "1.0.0",
            "eas_build_id": None,
            "eas_update_id": None,
            "eas_update_group_id": None,
        },
    }
    download = await client.get(f"/app-releases/{release_id}/download")
    assert download.status_code == 200
    assert download.headers["x-accel-redirect"] == (
        f"/_release_files/android/sha256/{_sha256()}.apk"
    )
    stored_artifact = tmp_path / "releases" / "android" / "sha256" / f"{_sha256()}.apk"
    assert stored_artifact.read_bytes() == APK_BYTES
    assert hashlib.sha256(stored_artifact.read_bytes()).hexdigest() == _sha256()

    withdrawn = await client.post(
        f"/internal/app-releases/{release_id}/withdraw",
        headers=release_headers,
        json={"reason": "broken startup"},
    )
    assert withdrawn.status_code == 200
    assert (await client.get(f"/app-releases/{release_id}/download")).status_code == 410
    after_withdrawal = await client.get(
        "/app-releases/android/latest",
        params={
            "channel": "production-direct",
            "current_version_code": version_code - 1,
            "runtime_version": "1.0.0",
        },
    )
    assert after_withdrawal.status_code == 200
    assert after_withdrawal.json()["update_available"] is False


@pytest.mark.asyncio
async def test_webhook_checks_hmac_before_json_parsing_and_rejects_invalid_pushes(client):
    invalid_signature = await client.post(
        "/webhooks/github/mobile-push",
        content=b"not-json",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-invalid-signature",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert invalid_signature.status_code == 401

    source_commit = _commit("invalid-push")
    cases = [
        {"repository": {"full_name": "other/repo"}},
        {"ref": "refs/heads/feature"},
        {"deleted": True},
        {"after": source_commit.upper()},
        {"event": "ping"},
    ]
    for index, changes in enumerate(cases):
        response = await _signed_push(
            client,
            source_commit=source_commit,
            delivery_id=f"delivery-invalid-{index}",
            **changes,
        )
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_webhook_delivery_is_idempotent_and_updates_both_lanes(client, release_headers):
    source_commit = _commit("duplicate-delivery")
    first = await _signed_push(client, source_commit=source_commit, delivery_id="delivery-duplicate")
    second = await _signed_push(client, source_commit=source_commit, delivery_id="delivery-duplicate")
    assert first.status_code == second.status_code == 200
    assert second.json()["duplicate"] is True

    for channel in ("production-direct", "production-play"):
        ledger = await client.get(
            f"/internal/app-releases/lanes/android/{channel}", headers=release_headers
        )
        assert ledger.status_code == 200
        assert ledger.json()["expected_source_commit"] == source_commit
        assert ledger.json()["expected_ci_run_id"] is None


@pytest.mark.asyncio
async def test_webhook_rejects_delayed_old_delivery_after_newer_push(client):
    commit_a = _commit("delivery-a")
    commit_b = _commit("delivery-b")
    assert (
        await _signed_push(client, source_commit=commit_a, delivery_id="delivery-a")
    ).status_code == 200
    assert (
        await _signed_push(
            client,
            source_commit=commit_b,
            before=commit_a,
            delivery_id="delivery-b",
        )
    ).status_code == 200
    delayed_a = await _signed_push(
        client, source_commit=commit_a, before="0" * 40, delivery_id="delivery-a"
    )
    assert delayed_a.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_github_pushes_serialize_before_reading_release_targets(db, monkeypatch):
    """A queued successor sees A's commit, never a stale pre-A lane snapshot."""

    commit_a = _commit("concurrent-delivery-a")
    commit_b = _commit("concurrent-delivery-b")
    first_lane_read = asyncio.Event()
    release_a = asyncio.Event()
    b_waiting_on_lock = asyncio.Event()
    original_lane = release_registry._lane
    original_lock_lane = release_registry._lock_lane

    async def pause_a_after_locks(session, platform, channel):
        if getattr(session, "_pause_github_target_read", False) and not first_lane_read.is_set():
            first_lane_read.set()
            await release_a.wait()
        return await original_lane(session, platform, channel)

    monkeypatch.setattr(release_registry, "_lane", pause_a_after_locks)

    async def signal_b_at_advisory_lock(session, platform, channel):
        if getattr(session, "_signal_github_lock", False) and not b_waiting_on_lock.is_set():
            b_waiting_on_lock.set()
        await original_lock_lane(session, platform, channel)

    monkeypatch.setattr(release_registry, "_lock_lane", signal_b_at_advisory_lock)

    async def apply_a():
        async with SessionLocal() as session:
            session._pause_github_target_read = True
            return await advance_github_mobile_push_targets(
                session, before="0" * 40, source_commit=commit_a
            )

    async def apply_b():
        async with SessionLocal() as session:
            session._signal_github_lock = True
            return await advance_github_mobile_push_targets(
                session, before=commit_a, source_commit=commit_b
            )

    a_task = asyncio.create_task(apply_a())
    await first_lane_read.wait()
    b_task = asyncio.create_task(apply_b())
    await b_waiting_on_lock.wait()
    assert not b_task.done()

    release_a.set()
    assert await a_task is False
    assert await b_task is False

    lanes = (
        await db.execute(
            select(AppReleaseLane).where(AppReleaseLane.platform == "android")
        )
    ).scalars().all()
    assert {(lane.channel, lane.expected_source_commit, lane.expected_ci_run_id) for lane in lanes} == {
        ("production-direct", commit_b, None),
        ("production-play", commit_b, None),
    }


@pytest.mark.asyncio
async def test_internal_routes_reject_missing_wrong_and_firebase_only_bearers(client):
    path = "/internal/app-releases/lanes/android/production-direct"
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer firebase-token"}):
        assert (await client.get(path, headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_direct_publish_rejects_stale_commit_and_hash_mismatch(client, release_headers):
    expected = _commit("expected")
    await _signed_push(client, source_commit=expected, delivery_id="delivery-expected")

    stale = await client.post(
        "/internal/app-releases/android/direct-apk",
        headers={
            **release_headers,
            "Idempotency-Key": "direct:stale",
            "X-Artifact-SHA256": _sha256(),
        },
        data=_metadata(source_commit=_commit("stale"), ci_run_id="delivery-expected", version_code=102),
        files={"artifact": ("eurith.apk", APK_BYTES, "application/vnd.android.package-archive")},
    )
    assert stale.status_code == 409

    mismatch = await client.post(
        "/internal/app-releases/android/direct-apk",
        headers={
            **release_headers,
            "Idempotency-Key": "direct:mismatch",
            "X-Artifact-SHA256": "0" * 64,
        },
        data=_metadata(source_commit=expected, ci_run_id="delivery-expected", version_code=102),
        files={"artifact": ("eurith.apk", APK_BYTES, "application/vnd.android.package-archive")},
    )
    assert mismatch.status_code == 422


@pytest.mark.asyncio
async def test_direct_publish_is_idempotent_and_rejects_conflicting_retry(client, release_headers):
    source_commit = _commit("idempotency")
    delivery_id = "delivery-idempotency"
    await _signed_push(client, source_commit=source_commit, delivery_id=delivery_id)
    headers = {
        **release_headers,
        "Idempotency-Key": "direct:idempotency",
        "X-Artifact-SHA256": _sha256(),
    }
    payload = _metadata(source_commit=source_commit, ci_run_id=delivery_id, version_code=103)
    assert (
        await _bind_ci_run(
            client, release_headers, source_commit=source_commit, ci_run_id=delivery_id
        )
    ).status_code == 200
    first = await client.post(
        "/internal/app-releases/android/direct-apk", headers=headers, data=payload,
        files={"artifact": ("eurith.apk", APK_BYTES, "application/vnd.android.package-archive")},
    )
    retry = await client.post(
        "/internal/app-releases/android/direct-apk", headers=headers, data=payload,
        files={"artifact": ("eurith.apk", APK_BYTES, "application/vnd.android.package-archive")},
    )
    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json()["release"]["id"] == first.json()["release"]["id"]

    conflicting_payload = {**payload, "version_name": "1.0.1"}
    conflicting = await client.post(
        "/internal/app-releases/android/direct-apk", headers=headers, data=conflicting_payload,
        files={"artifact": ("eurith.apk", APK_BYTES, "application/vnd.android.package-archive")},
    )
    assert conflicting.status_code == 409


@pytest.mark.asyncio
async def test_eas_ledger_mandatory_and_concurrent_publish(client, release_headers):
    source_commit = _commit("eas-and-concurrency")
    delivery_id = "delivery-eas-and-concurrency"
    await _signed_push(client, source_commit=source_commit, delivery_id=delivery_id)
    bound = await _bind_ci_run(
        client, release_headers, source_commit=source_commit, ci_run_id=delivery_id
    )
    assert bound.status_code == 200
    assert (
        await _bind_ci_run(
            client, release_headers, source_commit=source_commit, ci_run_id=delivery_id
        )
    ).status_code == 200
    assert (
        await _bind_ci_run(
            client, release_headers, source_commit=source_commit, ci_run_id="another-run"
        )
    ).status_code == 200
    assert (
        await _bind_ci_run(
            client, release_headers, source_commit=_commit("wrong-target"), ci_run_id="wrong"
        )
    ).status_code == 409

    exact_update_id = str(uuid4())
    update_group_id = str(uuid4())
    eas = await client.post(
        "/internal/app-releases/android/eas-update",
        headers={**release_headers, "Idempotency-Key": "eas:one"},
        json={
            "channel": "production-direct",
            "source_commit": source_commit,
            "ci_run_id": "another-run",
            "version_code": 104,
            "version_name": "1.0.0",
            "runtime_version": "1.0.0",
            "fingerprint": "fingerprint-1",
            "eas_update_id": exact_update_id,
            "eas_update_group_id": update_group_id,
            "release_notes": {"ru": "Исправления", "en": "Fixes"},
            "min_supported_version_code": 104,
        },
    )
    assert eas.status_code == 201
    assert eas.json()["release"]["eas_update_id"] == exact_update_id
    eas_id = eas.json()["release"]["id"]
    duplicate_exact_update = await client.post(
        "/internal/app-releases/android/eas-update",
        headers={**release_headers, "Idempotency-Key": "eas:duplicate-update-id"},
        json={
            "channel": "production-direct",
            "source_commit": source_commit,
            "ci_run_id": "another-run",
            "version_code": 104,
            "version_name": "1.0.0",
            "runtime_version": "1.0.0",
            "fingerprint": "fingerprint-1",
            "eas_update_id": exact_update_id,
            "eas_update_group_id": str(uuid4()),
            "release_notes": {"ru": "Исправления", "en": "Fixes"},
            "min_supported_version_code": 104,
        },
    )
    assert duplicate_exact_update.status_code == 409
    eas_latest = await client.get(
        "/app-releases/android/latest",
        params={
            "channel": "production-direct",
            "current_version_code": 104,
            "runtime_version": "1.0.0",
        },
    )
    assert eas_latest.status_code == 200
    assert eas_latest.json()["release"]["id"] == eas_id
    assert eas_latest.json()["release"]["min_supported_version_code"] == 104
    assert eas_latest.json()["mandatory"] is False
    not_manual = await client.patch(
        f"/internal/app-releases/{eas_id}/mandatory",
        headers={**release_headers, "X-Release-Manual-Operation": "true"},
        json={"mandatory": True},
    )
    assert not_manual.status_code == 401
    manual = await client.patch(
        f"/internal/app-releases/{eas_id}/mandatory",
        headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
        json={"mandatory": True},
    )
    assert manual.status_code == 200
    mandatory_latest = await client.get(
        "/app-releases/android/latest",
        params={
            "channel": "production-direct",
            "current_version_code": 104,
            "runtime_version": "1.0.0",
        },
    )
    assert mandatory_latest.status_code == 200
    assert mandatory_latest.json()["mandatory"] is True

    headers = {
        **release_headers,
        "Idempotency-Key": "direct:concurrent",
        "X-Artifact-SHA256": _sha256(),
    }
    payload = _metadata(source_commit=source_commit, ci_run_id="another-run", version_code=105)

    async def publish():
        return await client.post(
            "/internal/app-releases/android/direct-apk", headers=headers, data=payload,
            files={"artifact": ("eurith.apk", APK_BYTES, "application/vnd.android.package-archive")},
        )

    responses = await asyncio.gather(publish(), publish())
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert len({response.json()["release"]["id"] for response in responses}) == 1


@pytest.mark.asyncio
async def test_retry_binding_revokes_old_run_and_accepts_new_run(client, release_headers):
    source_commit = _commit("retry-binding")
    first_run = "retry-binding-first"
    newest_run = "retry-binding-newest"
    assert (
        await _signed_push(
            client, source_commit=source_commit, delivery_id="retry-binding-push"
        )
    ).status_code == 200

    first = await _bind_ci_run(
        client, release_headers, source_commit=source_commit, ci_run_id=first_run
    )
    retry = await _bind_ci_run(
        client, release_headers, source_commit=source_commit, ci_run_id=newest_run
    )
    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["expected_ci_run_id"] == newest_run

    def eas_payload(ci_run_id: str, group_id: str) -> dict:
        return {
            "channel": "production-direct",
            "source_commit": source_commit,
            "ci_run_id": ci_run_id,
            "version_code": 106,
            "version_name": "1.0.0",
            "runtime_version": "1.0.0",
            "fingerprint": "retry-binding-fingerprint",
            "eas_update_id": f"update-{group_id}",
            "eas_update_group_id": group_id,
            "release_notes": {"ru": "Исправления", "en": "Fixes"},
        }

    old_publish = await client.post(
        "/internal/app-releases/android/eas-update",
        headers={**release_headers, "Idempotency-Key": "retry-binding:old"},
        json=eas_payload(first_run, str(uuid4())),
    )
    new_publish = await client.post(
        "/internal/app-releases/android/eas-update",
        headers={**release_headers, "Idempotency-Key": "retry-binding:new"},
        json=eas_payload(newest_run, str(uuid4())),
    )

    assert old_publish.status_code == 409
    assert new_publish.status_code == 201


@pytest.mark.asyncio
async def test_bind_and_old_publish_share_lane_lock_in_both_commit_orders(
    db, monkeypatch
):
    """Prove retry takeover and old publication are one serializable decision."""

    source_commit = _commit("bind-publish-lock-race")
    original_lock = release_registry._lock_lane
    watched_session = None
    lock_attempted = asyncio.Event()

    async def instrumented_lock(session, platform: str, channel: str):
        if session is watched_session:
            lock_attempted.set()
        await original_lock(session, platform, channel)

    monkeypatch.setattr(release_registry, "_lock_lane", instrumented_lock)

    def command(channel: str, ci_run_id: str, suffix: str) -> dict:
        return {
            "platform": "android",
            "channel": channel,
            "source_commit": source_commit,
            "ci_run_id": ci_run_id,
            "version_code": 1,
            "version_name": "1.0.0",
            "runtime_version": None,
            "fingerprint": f"race-{suffix}",
            "release_notes": {"ru": "Исправления", "en": "Fixes"},
            "min_supported_version_code": None,
            "eas_build_id": None,
            "idempotency_key": f"race-{suffix}",
        }

    stored = {
        "storage_key": "android/sha256/" + "7" * 64 + ".apk",
        "sha256": "7" * 64,
        "size_bytes": 128,
    }

    # Linearization 1: retry binding commits first. The old run waits on the
    # real PostgreSQL advisory lock, then observes the replacement and fails.
    bind_first_channel = "production-direct"
    await release_registry.set_expected_commit(
        db, "android", bind_first_channel, source_commit, "old-run"
    )
    await db.commit()
    async with SessionLocal() as binder, SessionLocal() as old_publisher:
        watched_session = old_publisher
        lock_attempted = asyncio.Event()
        async with binder.begin():
            await release_registry.bind_expected_ci_run(
                binder, "android", bind_first_channel, source_commit, "new-run"
            )
            old_publish = asyncio.create_task(
                release_registry.publish_direct_release(
                    old_publisher,
                    command(bind_first_channel, "old-run", "bind-first"),
                    stored,
                )
            )
            await asyncio.wait_for(lock_attempted.wait(), timeout=2)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(old_publish), timeout=0.1)
        with pytest.raises(release_registry.StaleReleaseError):
            await asyncio.wait_for(old_publish, timeout=2)

    # Linearization 2: old publication commits first. The retry waits on the
    # same lock, then replaces the binding after the release is durable.
    publish_first_channel = "production-play"
    await release_registry.set_expected_commit(
        db, "android", publish_first_channel, source_commit, "old-run"
    )
    await db.commit()
    async with SessionLocal() as old_publisher, SessionLocal() as binder:
        watched_session = binder
        lock_attempted = asyncio.Event()
        async with old_publisher.begin():
            published = await release_registry.publish_direct_release(
                old_publisher,
                command(publish_first_channel, "old-run", "publish-first"),
                stored,
            )
            retry_bind = asyncio.create_task(
                release_registry.bind_expected_ci_run(
                    binder, "android", publish_first_channel, source_commit, "new-run"
                )
            )
            await asyncio.wait_for(lock_attempted.wait(), timeout=2)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(retry_bind), timeout=0.1)
        rebound = await asyncio.wait_for(retry_bind, timeout=2)

    assert published.created is True
    assert rebound.expected_ci_run_id == "new-run"
    async with SessionLocal() as verification:
        lanes = (
            await verification.execute(
                select(AppReleaseLane).where(
                    AppReleaseLane.channel.in_([bind_first_channel, publish_first_channel])
                )
            )
        ).scalars().all()
        releases = (
            await verification.execute(
                select(AppRelease).where(
                    AppRelease.channel.in_([bind_first_channel, publish_first_channel])
                )
            )
        ).scalars().all()

    assert {lane.channel: lane.expected_ci_run_id for lane in lanes} == {
        bind_first_channel: "new-run",
        publish_first_channel: "new-run",
    }
    assert [(release.channel, release.ci_run_id) for release in releases] == [
        (publish_first_channel, "old-run")
    ]
