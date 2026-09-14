from __future__ import annotations

import importlib
from types import SimpleNamespace

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


def test_empty_caddy_database_gets_current_schema_before_api_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty Caddy DB must not traverse the production-only Alembic baseline."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    metadata = MetaData()
    Table("app_releases", metadata, Column("id", Integer, primary_key=True))
    database = SimpleNamespace(engine=SimpleNamespace(begin=lambda: _BeginAdapter(engine)))
    models = SimpleNamespace(Base=SimpleNamespace(metadata=metadata))
    real_import = importlib.import_module

    def import_for_harness(name: str):
        if name == "app.database":
            return database
        if name == "api.services.models":
            return models
        if name == "api.main":
            assert inspect(engine).has_table("app_releases")
            raise _SchemaReady()
        return real_import(name)

    monkeypatch.setattr(caddy_harness, "importlib", SimpleNamespace(import_module=import_for_harness))
    monkeypatch.setattr(caddy_harness.CaddyHarness, "_prepare_files_and_mount", lambda self: None)

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
