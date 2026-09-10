"""Connection-pinned lock shared by release publishing and volume cleanup."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


# Fixed signed bigint: this is intentionally one global critical section for
# direct-APK publication and cleanup, not a per-lane transaction lock.
RELEASE_ARTIFACT_LOCK_KEY = -613_777_772_007_134_079


@asynccontextmanager
async def hold_release_artifact_lock(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Keep a session advisory lock through commits and filesystem operations.

    The connection is owned by this context, so a pool checkout after commit
    cannot accidentally unlock a different PostgreSQL session.
    """
    async with engine.connect() as connection:
        await connection.execute(
            text("SELECT pg_advisory_lock(:key)"),
            {"key": RELEASE_ARTIFACT_LOCK_KEY},
        )
        # ``execute`` autobegins on SQLAlchemy connections. Commit that empty
        # transaction without releasing the session-level lock, so each bound
        # AsyncSession below owns and commits its real registry transaction.
        await connection.commit()
        try:
            yield connection
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": RELEASE_ARTIFACT_LOCK_KEY},
            )
