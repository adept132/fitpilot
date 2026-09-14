from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect
from sqlalchemy.pool import StaticPool

from tests.deploy import caddy_harness


class _ConnectionAdapter:
    def __init__(self, connection):
        self.connection = connection

    async def run_sync(self, function):
        return function(self.connection)


class _BeginAdapter:
    def __init__(self, engine):
        self.context = engine.begin()

    async def __aenter__(self):
        return _ConnectionAdapter(self.context.__enter__())

    async def __aexit__(self, kind, value, traceback):
        return self.context.__exit__(kind, value, traceback)


def test_api_lifespan_owns_database_on_one_loop(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """Schema, fixture rows, requests, and teardown share the API worker loop."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    metadata = MetaData()
    Table("app_releases", metadata, Column("id", Integer, primary_key=True))
    events = []

    def create_schema(connection):
        events.append(("schema", asyncio.get_running_loop()))
        metadata.create_all(connection)

    async def dispose():
        events.append(("dispose", asyncio.get_running_loop()))
        engine.dispose()

    async def seed_rows(self, session_factory, app_release):
        assert inspect(engine).has_table("app_releases")
        events.append(("seed", asyncio.get_running_loop()))
        self.release_ids["valid"] = uuid4()
        self._session_factory = session_factory
        self._app_release = app_release

    async def delete_rows(self):
        events.append(("delete", asyncio.get_running_loop()))

    router = APIRouter()

    @router.get("/loop-probe")
    async def loop_probe():
        events.append(("request", asyncio.get_running_loop()))
        return {"ok": True}

    database = SimpleNamespace(
        engine=SimpleNamespace(begin=lambda: _BeginAdapter(engine), dispose=dispose),
        SessionLocal=object(),
    )
    models = SimpleNamespace(
        Base=SimpleNamespace(metadata=SimpleNamespace(create_all=create_schema)),
        AppRelease=object(),
    )
    real_import = importlib.import_module

    def import_for_harness(name: str):
        if name == "api.main":
            raise AssertionError("Caddy harness imported the credential-dependent full app")
        if name == "api.routers.releases":
            return SimpleNamespace(router=router)
        return real_import(name)

    monkeypatch.setattr(caddy_harness, "importlib", SimpleNamespace(import_module=import_for_harness))
    monkeypatch.setattr(caddy_harness.CaddyHarness, "_seed", seed_rows)
    monkeypatch.setattr(caddy_harness.CaddyHarness, "_delete_rows", delete_rows)

    def no_historical_migration(args, **kwargs):
        if args[:2] == ["docker", "rm"]:
            return SimpleNamespace(returncode=0)
        raise AssertionError("Caddy harness attempted a historical migration on an empty database")

    monkeypatch.setattr(caddy_harness, "_run", no_historical_migration)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody@localhost/fitpilot_task_caddy_test")
    harness = caddy_harness.CaddyHarness("postgresql+asyncpg://nobody@localhost/fitpilot_task_caddy_test")
    try:
        with TestClient(harness._build_api_app(database, models)) as client:
            assert client.get("/loop-probe").status_code == 200
    finally:
        harness.close()
    assert [name for name, _loop in events] == ["schema", "seed", "request", "delete", "dispose"]
    assert len({id(loop) for _name, loop in events}) == 1
    assert "ProgrammingError" not in caplog.text


@pytest.fixture
def lifecycle_harness(monkeypatch):
    """Keep the real lifespan/Uvicorn, replace only external DB and host resources."""
    harness = caddy_harness.CaddyHarness(
        "postgresql+asyncpg://localhost/fitpilot_task_caddy_lifecycle"
    )
    events = []

    @contextlib.asynccontextmanager
    async def begin():
        async def run_sync(_create):
            events.append("schema")
        yield SimpleNamespace(run_sync=run_sync)

    async def dispose():
        events.append("dispose")

    async def seed(_session_factory, _app_release):
        events.append("seed")
        harness.release_ids["valid"] = uuid4()

    async def delete():
        assert harness.release_ids
        events.append("delete")

    database = SimpleNamespace(
        engine=SimpleNamespace(begin=begin, dispose=dispose), SessionLocal=object()
    )
    models = SimpleNamespace(
        Base=SimpleNamespace(metadata=SimpleNamespace(create_all=object())), AppRelease=object()
    )
    real_import = importlib.import_module

    def imports(name):
        if name == "api.main":
            raise AssertionError("must not load full app")
        if name == "app.database":
            return database
        if name == "api.services.models":
            return models
        if name == "api.routers.releases":
            return SimpleNamespace(router=APIRouter())
        return real_import(name)

    monkeypatch.setattr(caddy_harness, "importlib", SimpleNamespace(import_module=imports))
    monkeypatch.setattr(caddy_harness, "_run", lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(harness, "_prepare_files_and_mount", lambda: None)
    monkeypatch.setattr(harness, "_start_caddy", lambda: None)
    monkeypatch.setattr(harness, "_seed", seed)
    monkeypatch.setattr(harness, "_delete_rows", delete)
    monkeypatch.setenv("DATABASE_URL", harness.database_url)
    monkeypatch.setenv("RELEASE_STORAGE_ROOT", str(harness.source_root))
    yield harness, database, models, events
    if harness.server is not None:
        harness.server.should_exit = True
    if harness.thread is not None:
        harness.thread.join(timeout=10)
    harness.temp.cleanup()


def test_partial_seed_failure_still_deletes_owned_rows(lifecycle_harness, monkeypatch):
    """Recording ownership after seeding strands rows on a partial commit failure."""
    harness, database, models, events = lifecycle_harness

    async def partial_seed(_factory, _model):
        harness.release_ids["partial"] = uuid4()
        raise RuntimeError("seed failed")

    monkeypatch.setattr(harness, "_seed", partial_seed)
    with pytest.raises(RuntimeError, match="seed failed"):
        with TestClient(harness._build_api_app(database, models)):
            pass
    assert events[-2:] == ["delete", "dispose"]


def test_worker_cleanup_failure_fails_caller_and_redacts_diagnostics(lifecycle_harness, monkeypatch, caplog):
    """Uvicorn logging shutdown failure must not turn failed deletion into success."""
    harness, _database, _models, events = lifecycle_harness

    async def failed_delete():
        events.append("delete")
        raise RuntimeError(
            "delete failed postgresql+asyncpg://user:private-password@localhost/fitpilot_task_caddy_x "
            "/_release_files/private-handoff.apk"
        )

    monkeypatch.setattr(harness, "_delete_rows", failed_delete)
    harness.start()
    with pytest.raises(RuntimeError, match="delete failed") as failure:
        harness.close()
    diagnostics = "".join(traceback.format_exception(failure.value)) + caplog.text
    assert "private-password" not in diagnostics
    assert "private-handoff.apk" not in diagnostics
    assert events[-2:] == ["delete", "dispose"]
    assert not harness.root.exists()


def test_worker_exit_before_readiness_fails_without_http_wait(lifecycle_harness, monkeypatch):
    """A dead worker must be detected before the 20-second HTTP readiness timeout."""
    harness, _database, _models, _events = lifecycle_harness
    worker = SimpleNamespace(start=lambda: None, is_alive=lambda: False, join=lambda **kw: None)
    monkeypatch.setattr(caddy_harness.threading, "Thread", lambda **kw: worker)

    def no_http(*args, **kwargs):
        pytest.fail("HTTP attempted despite already exited API worker")

    monkeypatch.setattr(caddy_harness.http.client, "HTTPConnection", no_http)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="API exited before readiness"):
        harness.start()
    assert time.monotonic() - started < 2


def test_close_reports_live_worker_after_releasing_other_resources(lifecycle_harness):
    harness, _database, _models, events = lifecycle_harness
    harness.server = SimpleNamespace(should_exit=False)
    harness.thread = SimpleNamespace(
        join=lambda timeout: events.append(("join", timeout)), is_alive=lambda: True
    )
    with pytest.raises(RuntimeError, match="did not stop within 10 seconds"):
        harness.close()
    assert harness.server.should_exit
    assert ("join", 10) in events
    assert not harness.root.exists()


def test_gate_preserves_original_test_error_with_cleanup_failure(lifecycle_harness, monkeypatch):
    harness, _database, _models, _events = lifecycle_harness

    async def failed_delete():
        raise RuntimeError("delete failed")

    monkeypatch.setattr(harness, "_delete_rows", failed_delete)
    monkeypatch.setattr(caddy_harness.CaddyHarness, "_unavailable_reason", staticmethod(lambda: None))
    monkeypatch.setenv("TEST_DATABASE_URL", harness.database_url)
    class GateHarness(caddy_harness.CaddyHarness):
        def __new__(cls, url):
            return harness

    with pytest.raises(BaseException) as failure:
        with GateHarness.start_or_skip():
            raise AssertionError("original request assertion")
    diagnostics = "".join(traceback.format_exception(failure.value))
    assert "original request assertion" in diagnostics
    assert "delete failed" in diagnostics


@pytest.mark.parametrize("phase", ["schema", "seed", "dispose"])
def test_lifespan_failure_reaches_caller_even_when_uvicorn_swallows_it(lifecycle_harness, monkeypatch, phase):
    harness, database, _models, events = lifecycle_harness

    async def fail(*_args):
        raise RuntimeError(f"{phase} failed")

    if phase == "schema":
        @contextlib.asynccontextmanager
        async def failed_begin():
            raise RuntimeError("schema failed")
            yield
        database.engine.begin = failed_begin
    elif phase == "seed":
        monkeypatch.setattr(harness, "_seed", fail)
    else:
        database.engine.dispose = fail

    started = time.monotonic()
    if phase == "dispose":
        harness.start()
    else:
        with pytest.raises(RuntimeError, match=f"{phase} failed"):
            harness.start()
        assert time.monotonic() - started < 5
        assert "dispose" in events
    with pytest.raises(RuntimeError, match=f"{phase} failed"):
        harness.close()
    assert not harness.root.exists()


def test_failed_unmount_keeps_owned_view_and_reports_failure(lifecycle_harness, monkeypatch):
    harness, _database, _models, _events = lifecycle_harness
    harness._mounted = True

    def failed_unmount(args, **kwargs):
        return SimpleNamespace(returncode=1 if args[0] == "umount" else 0, stderr=b"busy")

    monkeypatch.setattr(caddy_harness, "_run", failed_unmount)
    with pytest.raises(RuntimeError, match="unmount"):
        harness.close()
    assert harness._mounted
    assert harness.root.exists()


def test_container_cleanup_error_does_not_prevent_worker_shutdown(lifecycle_harness, monkeypatch):
    harness, _database, _models, events = lifecycle_harness
    harness._container_attempted = True
    harness.server = SimpleNamespace(should_exit=False)
    harness.thread = SimpleNamespace(
        join=lambda timeout: events.append("join"), is_alive=lambda: False
    )

    def failed_cleanup(*args, **kwargs):
        raise RuntimeError("docker cleanup failed")

    monkeypatch.setattr(caddy_harness, "_run", failed_cleanup)
    with pytest.raises(RuntimeError, match="docker cleanup failed"):
        harness.close()
    assert harness.server.should_exit
    assert "join" in events
    assert not harness.root.exists()


def test_factory_crash_is_sanitized_and_fails_readiness(lifecycle_harness, monkeypatch, caplog):
    harness, _database, _models, _events = lifecycle_harness

    def failed_factory(*args):
        raise RuntimeError("factory failed /_release_files/factory-secret.apk")

    monkeypatch.setattr(harness, "_build_api_app", failed_factory)
    with pytest.raises(RuntimeError, match="factory failed") as failure:
        harness.start()
    assert "factory-secret.apk" not in str(failure.value) + caplog.text
    with pytest.raises(RuntimeError, match="factory failed"):
        harness.close()
    assert not harness.root.exists()


@pytest.mark.parametrize("failure_command", ["remount", "findmnt"])
def test_mount_setup_failure_still_unmounts_owned_bind(lifecycle_harness, monkeypatch, failure_command):
    """A bind exists even if its protection or inspection step fails afterward."""
    harness, _database, _models, events = lifecycle_harness

    def mount_commands(args, **kwargs):
        if args[0] == "umount":
            events.append(("unmount", args[1]))
        elif (failure_command == "remount" and args[:2] == ["mount", "-o"]) or (
            failure_command == "findmnt" and args[0] == "findmnt"
        ):
            raise RuntimeError("mount setup failed")
        return SimpleNamespace(returncode=0, stdout=b"ro,nosymfollow")

    monkeypatch.setattr(caddy_harness, "_run", mount_commands)
    with pytest.raises(RuntimeError, match="mount setup failed"):
        harness._mount_protected(harness.final_source, harness.view_final)
    harness.close()
    assert ("unmount", str(harness.view_final)) in events
    assert not harness.root.exists()


def test_public_release_route_loads_without_firebase_credentials() -> None:
    """A public APK request must not initialize Firebase at import time."""
    env = os.environ.copy()
    env.pop("FIREBASE_CREDENTIALS", None)
    env["DATABASE_URL"] = "postgresql+asyncpg://nobody@localhost/fitpilot_task_caddy_test"
    script = """
import sys
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.routers.releases import router
assert "api.core.firebase_admin" not in sys.modules
app = FastAPI()
app.include_router(router)
response = TestClient(app).get("/app-releases/not-a-uuid/download")
assert response.status_code == 422, response.status_code
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_startup_account_deletion_initializes_firebase_before_delete() -> None:
    """Deferred auth import must not strand accounts purged before first login."""
    env = os.environ.copy()
    env["DATABASE_URL"] = "postgresql+asyncpg://nobody@localhost/fitpilot_task_caddy_test"
    env["FIREBASE_CREDENTIALS"] = "{}"
    script = """
import asyncio
import firebase_admin
from firebase_admin import auth, credentials

firebase_admin._apps.clear()
initialized = []
credentials.Certificate = lambda _value: object()
def initialize(credential):
    initialized.append(credential)
    firebase_admin._apps["[DEFAULT]"] = object()
firebase_admin.initialize_app = initialize
def delete_user(uid):
    assert initialized
    assert uid == "expired-user"
auth.delete_user = delete_user

from api.services.account_service import delete_firebase_user
assert asyncio.run(delete_firebase_user("expired-user")) is True
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
