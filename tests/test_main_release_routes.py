"""Public Update Center contracts stay independent from Firebase auth."""

from __future__ import annotations

import os
import sys
import types
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


# The public-route tests deliberately exercise the real application router.
# Firebase only backs unrelated authenticated endpoints, so a test double keeps
# its import-time credential loading out of this public API contract.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:5432/test"
)
firebase_module = types.ModuleType("api.core.firebase_admin")
firebase_module.verify_firebase_token = lambda token: {"uid": "test"}
sys.modules.setdefault("api.core.firebase_admin", firebase_module)

from api.deps import get_db
from api.main import app


async def _fake_db():
    yield object()


@pytest.fixture(autouse=True)
def public_database_override():
    app.dependency_overrides[get_db] = _fake_db
    yield
    app.dependency_overrides.clear()


def _release(**overrides):
    values = {
        "id": uuid4(),
        "platform": "android",
        "channel": "production-direct",
        "delivery_method": "direct_apk",
        "version_code": 2,
        "version_name": "1.0.1",
        "runtime_version": "1.0.1",
        "fingerprint": "fingerprint",
        "release_notes": {"ru": "Исправления", "en": "Fixes"},
        "status": "published",
        "is_mandatory": False,
        "min_supported_version_code": None,
        "artifact_storage_key": "android/sha256/" + "a" * 64 + ".apk",
        "artifact_sha256": "a" * 64,
        "artifact_size_bytes": 12,
        "eas_update_group_id": None,
        "source_commit": "b" * 40,
        "published_at": "2026-09-02T10:00:00Z",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _router_module():
    return import_module("api.routers.releases")


def test_release_routes_are_registered_once():
    paths = [route.path for route in app.routes]
    assert paths.count("/app-releases/android/latest") == 1
    assert paths.count("/app-releases/{release_id}/download") == 1


def test_latest_is_public_and_no_store(monkeypatch):
    routes = _router_module()

    async def selected_instruction(_db, query):
        assert query.platform == "android"
        return SimpleNamespace(
            current_version_code=query.current_version_code,
            update_available=False,
            mandatory=False,
            current_release_withdrawn=False,
            release=None,
        )

    monkeypatch.setattr(routes, "latest_instruction", selected_instruction)
    response = TestClient(app).get(
        "/app-releases/android/latest",
        params={"channel": "production-direct", "current_version_code": 1},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["update_available"] is False
    assert response.json()["release"] is None


def test_latest_returns_direct_download_instruction(monkeypatch):
    routes = _router_module()
    release = _release()

    async def selected_instruction(_db, _query):
        return SimpleNamespace(
            current_version_code=1,
            update_available=True,
            mandatory=False,
            current_release_withdrawn=False,
            release=release,
        )

    monkeypatch.setattr(routes, "latest_instruction", selected_instruction)
    response = TestClient(app).get(
        "/app-releases/android/latest",
        params={"channel": "production-direct", "current_version_code": 1},
    )

    assert response.status_code == 200
    instruction = response.json()["release"]
    assert instruction["delivery_method"] == "direct_apk"
    assert instruction["download_url"].endswith(f"/app-releases/{release.id}/download")
    assert instruction["sha256"] == release.artifact_sha256
    assert instruction["size_bytes"] == release.artifact_size_bytes
    assert instruction["min_supported_version_code"] is None


def test_latest_exposes_minimum_supported_version_code(monkeypatch):
    routes = _router_module()
    release = _release(min_supported_version_code=2)

    async def selected_instruction(_db, _query):
        return SimpleNamespace(
            current_version_code=1,
            update_available=True,
            mandatory=True,
            current_release_withdrawn=False,
            release=release,
        )

    monkeypatch.setattr(routes, "latest_instruction", selected_instruction)
    response = TestClient(app).get(
        "/app-releases/android/latest",
        params={"channel": "production-direct", "current_version_code": 1},
    )

    assert response.status_code == 200
    assert response.json()["mandatory"] is True
    assert response.json()["release"]["min_supported_version_code"] == 2


def test_latest_returns_compatible_eas_instruction(monkeypatch):
    routes = _router_module()
    release = _release(
        delivery_method="eas_update",
        artifact_storage_key=None,
        artifact_sha256=None,
        artifact_size_bytes=None,
        eas_update_group_id="update-group-1",
        min_supported_version_code=2,
    )

    async def selected_instruction(_db, _query):
        return SimpleNamespace(
            current_version_code=2,
            update_available=True,
            mandatory=False,
            current_release_withdrawn=False,
            release=release,
        )

    monkeypatch.setattr(routes, "latest_instruction", selected_instruction)
    response = TestClient(app).get(
        "/app-releases/android/latest",
        params={
            "channel": "production-direct",
            "current_version_code": 2,
            "runtime_version": "1.0.1",
        },
    )

    assert response.status_code == 200
    instruction = response.json()["release"]
    assert instruction["delivery_method"] == "eas_update"
    assert instruction["eas_update_group_id"] == "update-group-1"
    assert instruction["download_url"] is None
    assert instruction["sha256"] is None
    assert instruction["min_supported_version_code"] == 2


def test_latest_rejects_unknown_channel_and_non_positive_version():
    client = TestClient(app)
    wrong_channel = client.get(
        "/app-releases/android/latest",
        params={"channel": "preview", "current_version_code": 1},
    )
    invalid_version = client.get(
        "/app-releases/android/latest",
        params={"channel": "production-direct", "current_version_code": 0},
    )

    assert wrong_channel.status_code == 422
    assert invalid_version.status_code == 422


def test_download_returns_accel_headers_for_existing_direct_apk(monkeypatch, tmp_path):
    routes = _router_module()
    release = _release()
    artifact = tmp_path / release.artifact_storage_key
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"A" * release.artifact_size_bytes)

    async def selected_release(_db, release_id):
        assert release_id == release.id
        return release

    monkeypatch.setattr(routes, "_release_by_id", selected_release)
    monkeypatch.setattr(routes, "release_storage_root", lambda: tmp_path)
    response = TestClient(app).get(f"/app-releases/{release.id}/download")

    assert response.status_code == 200
    assert response.headers["x-accel-redirect"] == f"/_release_files/{release.artifact_storage_key}"
    assert response.headers["content-type"] == "application/vnd.android.package-archive"
    assert response.headers["content-disposition"] == 'attachment; filename="eurith-1.0.1.apk"'
    assert response.headers["content-length"] == "12"
    assert response.headers["etag"] == f'"sha256:{release.artifact_sha256}"'
    assert response.headers["digest"].startswith("sha-256=")


@pytest.mark.parametrize(
    ("release", "expected_status", "expected_code"),
    [
        (None, 404, "release.not_found"),
        (_release(status="withdrawn"), 410, "release.withdrawn"),
        (_release(delivery_method="eas_update"), 404, "release.not_found"),
    ],
)
def test_download_uses_localized_legacy_compatible_errors(
    monkeypatch, release, expected_status, expected_code
):
    routes = _router_module()

    async def selected_release(_db, _release_id):
        return release

    monkeypatch.setattr(routes, "_release_by_id", selected_release)
    response = TestClient(app).get(f"/app-releases/{uuid4()}/download", headers={"Accept-Language": "en"})

    assert response.status_code == expected_status
    payload = response.json()
    assert isinstance(payload["detail"], str)
    assert payload["error"]["code"] == expected_code


def test_download_rejects_missing_or_unsafe_artifact_without_exposing_paths(monkeypatch, tmp_path):
    routes = _router_module()
    release = _release(artifact_storage_key="../../private.apk", version_name="1.0.1\r\nX-Evil: yes")

    async def selected_release(_db, _release_id):
        return release

    monkeypatch.setattr(routes, "_release_by_id", selected_release)
    monkeypatch.setattr(routes, "release_storage_root", lambda: tmp_path)
    response = TestClient(app).get(f"/app-releases/{release.id}/download")

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "release.artifact_unavailable"
    assert str(tmp_path) not in payload["detail"]
    assert "private.apk" not in payload["detail"]


def test_download_rejects_non_storage_generated_key(monkeypatch, tmp_path):
    routes = _router_module()
    release = _release(artifact_storage_key="android/unexpected.apk")
    artifact = tmp_path / release.artifact_storage_key
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"A" * release.artifact_size_bytes)

    async def selected_release(_db, _release_id):
        return release

    monkeypatch.setattr(routes, "_release_by_id", selected_release)
    monkeypatch.setattr(routes, "release_storage_root", lambda: tmp_path)
    response = TestClient(app).get(f"/app-releases/{release.id}/download")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "release.artifact_unavailable"


def test_download_returns_503_when_published_artifact_is_missing(monkeypatch, tmp_path):
    routes = _router_module()
    release = _release()

    async def selected_release(_db, _release_id):
        return release

    monkeypatch.setattr(routes, "_release_by_id", selected_release)
    monkeypatch.setattr(routes, "release_storage_root", lambda: tmp_path)
    response = TestClient(app).get(f"/app-releases/{release.id}/download")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "release.artifact_unavailable"


def test_download_rejects_file_with_unexpected_size(monkeypatch, tmp_path):
    routes = _router_module()
    release = _release()
    artifact = tmp_path / release.artifact_storage_key
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"truncated")

    async def selected_release(_db, _release_id):
        return release

    monkeypatch.setattr(routes, "_release_by_id", selected_release)
    monkeypatch.setattr(routes, "release_storage_root", lambda: tmp_path)
    response = TestClient(app).get(f"/app-releases/{release.id}/download")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "release.artifact_unavailable"


def test_download_filename_cannot_inject_response_headers(monkeypatch, tmp_path):
    routes = _router_module()
    release = _release(version_name="1.0.1\r\nX-Evil: yes")
    artifact = tmp_path / release.artifact_storage_key
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"A" * release.artifact_size_bytes)

    async def selected_release(_db, _release_id):
        return release

    monkeypatch.setattr(routes, "_release_by_id", selected_release)
    monkeypatch.setattr(routes, "release_storage_root", lambda: tmp_path)
    response = TestClient(app).get(f"/app-releases/{release.id}/download")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="eurith-update.apk"'
    assert "x-evil" not in response.headers


def test_download_validates_release_id_as_uuid():
    response = TestClient(app).get("/app-releases/not-a-uuid/download")
    assert response.status_code == 422
