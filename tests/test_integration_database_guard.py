from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "url",
    [
        "",
        "postgresql+asyncpg://localhost/fitpilot",
        "postgresql+asyncpg://db.internal/fitpilot_task7_unique",
        "postgresql+asyncpg://localhost/postgres",
        "postgresql+asyncpg://localhost/fitpilot_task7",
        "postgresql+asyncpg://localhost/fitpilot_task_caddy",
        "postgresql+asyncpg://localhost/fitpilot_task_caddy_",
        "postgresql+asyncpg://localhost/fitpilot_task_caddy-bad",
        "postgresql+asyncpg://db.internal/fitpilot_task_caddy_a1b2c3",
        "postgresql://localhost/fitpilot_task7_unique",
    ],
)
def test_integration_guard_rejects_missing_remote_or_nondisposable_database(url: str) -> None:
    """Catches integration collection importing the app against an ordinary database."""
    from tests.integration.database_test_guard import require_disposable_integration_database

    env = {"TEST_DATABASE_URL": url} if url else {}
    env["DATABASE_URL"] = "postgresql+asyncpg://localhost/fitpilot"

    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL"):
        require_disposable_integration_database(env)


@pytest.mark.parametrize(
    "database_name",
    [
        "fitpilot_integration_a1b2c3",
        "fitpilot_task7_a1b2c3",
        "fitpilot_task_caddy_a1b2c3",
    ],
)
def test_integration_guard_accepts_unique_local_disposable_database(database_name: str) -> None:
    """Catches a prefix policy that prevents the full suite from using its own task DB."""
    from tests.integration.database_test_guard import require_disposable_integration_database

    require_disposable_integration_database(
        {"TEST_DATABASE_URL": f"postgresql+asyncpg://localhost/{database_name}"}
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["pytest", "tests/integration"],
        ["pytest", "tests/integration", "-k", "release_cleanup"],
        ["pytest"],
    ],
)
def test_integration_conftest_cannot_bypass_guard_via_pytest_arguments(arguments: list[str]) -> None:
    """Catches an argv-dependent guard that misses directory, -k, or implicit collection."""
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("TEST_DATABASE_URL", None)
    env["DATABASE_URL"] = "postgresql+asyncpg://localhost/fitpilot"
    script = "import sys; sys.argv = " + repr(arguments) + "; import tests.integration.conftest"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode != 0
    assert "TEST_DATABASE_URL" in completed.stderr
