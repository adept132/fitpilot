from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
import subprocess
import sys
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


def test_api_lifespan_owns_database_on_one_loop(monkeypatch: pytest.MonkeyPatch) -> None:
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
        engine.dispose()
    assert [name for name, _loop in events] == ["schema", "seed", "request", "delete", "dispose"]
    assert len({id(loop) for _name, loop in events}) == 1


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
