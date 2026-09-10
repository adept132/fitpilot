"""Real h11 framing regression for the proxy-owned APK response body."""

from __future__ import annotations

import http.client
import logging
import os
import socket
import sys
import threading
import time
import types
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
import uvicorn


os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:5432/test"
)
firebase_module = types.ModuleType("api.core.firebase_admin")
firebase_module.verify_firebase_token = lambda token: {"uid": "test"}
sys.modules.setdefault("api.core.firebase_admin", firebase_module)

from api.deps import get_db
from api.main import app


def _release():
    return SimpleNamespace(
        id=uuid4(),
        delivery_method="direct_apk",
        version_name="1.0.1",
        status="published",
        artifact_storage_key="android/sha256/" + "a" * 64 + ".apk",
        artifact_sha256="a" * 64,
        artifact_size_bytes=12,
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until_started(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_download_handoff_has_valid_empty_h11_framing(
    monkeypatch, tmp_path, caplog, method
):
    routes = import_module("api.routers.releases")
    release = _release()
    artifact = tmp_path / release.artifact_storage_key
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"A" * release.artifact_size_bytes)

    async def fake_db():
        yield object()

    async def selected_release(_db, release_id):
        assert release_id == release.id
        return release

    app.dependency_overrides[get_db] = fake_db
    monkeypatch.setattr(routes, "_release_by_id", selected_release)
    monkeypatch.setattr(routes, "release_storage_root", lambda: tmp_path)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            http="h11",
            lifespan="off",
            log_level="warning",
            log_config=None,
        )
    )
    server.install_signal_handlers = lambda: None
    server_thread = threading.Thread(target=server.run, daemon=True)
    caplog.set_level(logging.WARNING)

    try:
        server_thread.start()
        _wait_until_started(server, server_thread)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, f"/app-releases/{release.id}/download")
            response = connection.getresponse()
            assert response.status == 200
            assert response.read() == b""
            assert response.getheader("Content-Length") is None
            assert response.getheader("X-Accel-Redirect") == (
                f"/_release_files/{release.artifact_storage_key}"
            )
        finally:
            connection.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)
        app.dependency_overrides.clear()

    assert not server_thread.is_alive()
    logs = caplog.text
    assert "LocalProtocolError" not in logs
    assert "Too little data for declared Content-Length" not in logs
