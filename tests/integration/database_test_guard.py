"""Fail-closed database boundary for every real-PostgreSQL integration test."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


_DISPOSABLE_DATABASE = re.compile(
    r"^fitpilot_(?:integration|task[0-9]+|task_caddy)_[a-z0-9][a-z0-9_]*$"
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def require_disposable_integration_database(
    env: dict[str, str] | os._Environ[str] | None = None,
) -> str:
    """Return an explicit local disposable URL or stop before app imports."""
    source = os.environ if env is None else env
    url = source.get("TEST_DATABASE_URL", "").strip()
    try:
        split = urlsplit(url)
        parsed = make_url(url)
        connect_args = parsed.translate_connect_args()
    except (ArgumentError, TypeError, ValueError):
        split = None
        parsed = None
        connect_args = {}
    database_name = connect_args.get("database", "")
    effective_host = connect_args.get("host", "")
    if (
        not url
        or split is None
        or parsed is None
        or split.query != ""
        or split.fragment != ""
        or parsed.drivername != "postgresql+asyncpg"
        or parsed.query
        or effective_host not in _LOCAL_HOSTS
        or _DISPOSABLE_DATABASE.fullmatch(database_name) is None
    ):
        raise RuntimeError(
            "integration tests require explicit TEST_DATABASE_URL on localhost "
            "for a unique fitpilot_integration_*, fitpilot_taskN_*, or "
            "fitpilot_task_caddy_* database"
        )
    return url
