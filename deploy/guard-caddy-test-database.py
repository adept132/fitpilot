#!/usr/bin/env python3
"""Fail closed before Caddy integration may connect to a database."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit


_DATABASE = re.compile(r"/fitpilot_task_caddy_[a-z0-9][a-z0-9_]*\Z")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def guarded_test_database_url(value: str) -> bool:
    if not value or len(value) > 4096 or not value.isascii() or any(
        ord(char) < 33 or ord(char) == 127 or char == "\\" for char in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError:
        return False
    return (
        parsed.scheme == "postgresql+asyncpg"
        and parsed.hostname in _LOCAL_HOSTS
        and (port is None or 1 <= port <= 65535)
        and not parsed.query
        and not parsed.fragment
        and _DATABASE.fullmatch(parsed.path) is not None
        and ((username is None and password is None and "@" not in parsed.netloc)
             or (bool(username) and bool(password) and parsed.netloc.count("@") == 1))
    )


if __name__ == "__main__":
    raise SystemExit(0 if guarded_test_database_url(os.environ.get("TEST_DATABASE_URL", "")) else 1)
