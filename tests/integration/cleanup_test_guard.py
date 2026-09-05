"""Early guard for the destructive release-cleanup PostgreSQL integration module."""
from __future__ import annotations

import os
from urllib.parse import urlparse


def require_disposable_cleanup_database(env: dict[str, str] | None = None) -> None:
    source = os.environ if env is None else env
    url = source.get("TEST_DATABASE_URL", "")
    parsed = urlparse(url.replace("postgresql+asyncpg", "postgresql", 1))
    if (
        not url
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or not parsed.path.lstrip("/").startswith("fitpilot_task7_")
    ):
        raise RuntimeError("release cleanup integration requires TEST_DATABASE_URL for a local fitpilot_task7_* database")
