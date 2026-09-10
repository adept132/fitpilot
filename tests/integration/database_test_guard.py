"""Fail-closed database boundary for every real-PostgreSQL integration test."""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse


_DISPOSABLE_DATABASE = re.compile(
    r"^fitpilot_(?:integration|task[0-9]+)_[a-z0-9][a-z0-9_]*$"
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def require_disposable_integration_database(
    env: dict[str, str] | os._Environ[str] | None = None,
) -> str:
    """Return an explicit local disposable URL or stop before app imports."""
    source = os.environ if env is None else env
    url = source.get("TEST_DATABASE_URL", "").strip()
    parsed = urlparse(url)
    database_name = parsed.path.lstrip("/")
    if (
        not url
        or parsed.scheme != "postgresql+asyncpg"
        or parsed.hostname not in _LOCAL_HOSTS
        or _DISPOSABLE_DATABASE.fullmatch(database_name) is None
    ):
        raise RuntimeError(
            "integration tests require explicit TEST_DATABASE_URL on localhost "
            "for a unique fitpilot_integration_* or fitpilot_taskN_* database"
        )
    return url
