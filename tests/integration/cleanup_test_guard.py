"""Early guard for the destructive release-cleanup PostgreSQL integration module."""
from __future__ import annotations

import os
from urllib.parse import urlparse

from tests.integration.database_test_guard import require_disposable_integration_database


def require_disposable_cleanup_database(env: dict[str, str] | None = None) -> None:
    source = os.environ if env is None else env
    url = require_disposable_integration_database(source)
    parsed = urlparse(url.replace("postgresql+asyncpg", "postgresql", 1))
    if (
        not url
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or not parsed.path.lstrip("/").startswith(
            ("fitpilot_task7_", "fitpilot_task8_")
        )
    ):
        raise RuntimeError(
            "release cleanup integration requires TEST_DATABASE_URL for a local "
            "fitpilot_task7_* or fitpilot_task8_* database"
        )
