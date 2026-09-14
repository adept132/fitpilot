from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect
from sqlalchemy.pool import StaticPool

from tests.deploy import caddy_harness


class _SchemaReady(Exception):
    pass


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


def test_caddy_schema_and_seed_share_one_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pooled DB connection must not cross asyncio.run event loops."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    metadata = MetaData()
    Table("app_releases", metadata, Column("id", Integer, primary_key=True))
    schema_loop = None
    deleted_rows = []
    dispose_calls = 0

    def create_schema(connection):
        nonlocal schema_loop
        schema_loop = asyncio.get_running_loop()
        metadata.create_all(connection)

    async def dispose():
        nonlocal dispose_calls
        if dispose_calls == 0:
            assert asyncio.get_running_loop() is schema_loop
        dispose_calls += 1

    async def seed_on_schema_loop(self, session_factory, app_release):
        assert inspect(engine).has_table("app_releases")
        assert asyncio.get_running_loop() is schema_loop
        self.release_ids["valid"] = uuid4()
        self._session_factory = session_factory
        self._app_release = app_release

    async def delete_rows(self):
        deleted_rows.append(True)

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
        if name == "app.database":
            return database
        if name == "api.services.models":
            return models
        if name == "api.main":
            raise AssertionError("Caddy harness imported the credential-dependent full app")
        if name == "api.routers.releases":
            assert inspect(engine).has_table("app_releases")
            raise _SchemaReady()
        return real_import(name)

    monkeypatch.setattr(caddy_harness, "importlib", SimpleNamespace(import_module=import_for_harness))
    monkeypatch.setattr(caddy_harness.CaddyHarness, "_prepare_files_and_mount", lambda self: None)
    monkeypatch.setattr(caddy_harness.CaddyHarness, "_seed", seed_on_schema_loop)
    monkeypatch.setattr(caddy_harness.CaddyHarness, "_delete_rows", delete_rows)

    def no_historical_migration(args, **kwargs):
        if args[:2] == ["docker", "rm"]:
            return SimpleNamespace(returncode=0)
        raise AssertionError("Caddy harness attempted a historical migration on an empty database")

    monkeypatch.setattr(caddy_harness, "_run", no_historical_migration)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody@localhost/fitpilot_task_caddy_test")
    harness = caddy_harness.CaddyHarness("postgresql+asyncpg://nobody@localhost/fitpilot_task_caddy_test")
    try:
        with pytest.raises(_SchemaReady):
            harness.start()
    finally:
        harness.close()
    assert deleted_rows == [True]


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
