import os
from pathlib import Path
import subprocess
import sys

import pytest


GUARD = Path(__file__).resolve().parents[2] / "deploy" / "guard-caddy-test-database.py"


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("postgresql+asyncpg://local_user:p%40ss@127.0.0.1:5432/fitpilot_task_caddy_a1b2", True),
        ("postgresql+asyncpg://localhost/fitpilot_task_caddy_a1b2", True),
        ("postgresql+asyncpg://local_user:p%40ss@127.0.0.1:5432/eurith", False),
        ("postgresql+asyncpg://local_user:p%40ss@db.example.com/fitpilot_task_caddy_a1b2", False),
        ("postgresql+asyncpg://local_user:p%40ss@127.0.0.1:5432/fitpilot_task_caddy_a1b2?sslmode=disable", False),
        ("postgresql+asyncpg://local_user:p%40ss@127.0.0.1:5432/fitpilot_task_caddy_%61", False),
    ],
)
def test_caddy_database_guard_accepts_only_local_disposable_database(url, allowed):
    completed = subprocess.run(
        [sys.executable, str(GUARD)],
        env={**os.environ, "TEST_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )

    assert (completed.returncode == 0) is allowed
    assert url not in completed.stdout + completed.stderr
