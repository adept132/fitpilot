#!/usr/bin/env python3
"""Probe health and the current code's complete ORM schema contract."""

from __future__ import annotations

import asyncio
import re
import sys

from sqlalchemy import text

from api.main import health_check
from api.services.models import Base
from app.database import SessionLocal, engine


REVISION = re.compile(r"^[0-9A-Za-z_]+$")


async def probe(expected_head: str) -> None:
    if not REVISION.fullmatch(expected_head):
        raise RuntimeError("expected_head_invalid")
    async with SessionLocal() as session:
        health = await health_check(session)
        if health != {"status": "ok", "database": "connected"}:
            raise RuntimeError("health_check_failed")
        actual_head = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        if actual_head != expected_head:
            raise RuntimeError("alembic_version_mismatch")
        unsafe_training_block_provenance = (await session.execute(text("""
            SELECT EXISTS (
                SELECT 1
                FROM training_blocks
                WHERE phase_snapshot_trusted = false
                  AND (
                    mesocycle_id IS NOT NULL
                    OR user_mesocycle_id IS NOT NULL
                    OR status = 'active'
                  )
            )
        """))).scalar_one()
        if unsafe_training_block_provenance:
            raise RuntimeError("unsafe_training_block_provenance")
        invalid_release_registry_row = (await session.execute(text("""
            SELECT EXISTS (
                SELECT 1
                FROM app_releases
                WHERE platform <> 'android'
                   OR channel NOT IN ('production-direct', 'production-play')
                   OR delivery_method NOT IN ('direct_apk', 'eas_update', 'google_play')
                   OR status NOT IN ('published', 'withdrawn')
            )
        """))).scalar_one()
        if invalid_release_registry_row:
            raise RuntimeError("invalid_release_registry_row")
        for table in Base.metadata.sorted_tables:
            exists = (await session.execute(
                text("SELECT to_regclass(:qualified) IS NOT NULL"),
                {"qualified": f"public.{table.name}"},
            )).scalar_one()
            if not exists:
                raise RuntimeError(f"missing_table:{table.name}")
            rows = await session.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:table"),
                {"table": table.name},
            )
            actual_columns = {row[0] for row in rows}
            missing = sorted(column.name for column in table.columns if column.name not in actual_columns)
            if missing:
                raise RuntimeError(f"missing_column:{table.name}:{','.join(missing)}")
    await engine.dispose()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        asyncio.run(probe(argv[1]))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
