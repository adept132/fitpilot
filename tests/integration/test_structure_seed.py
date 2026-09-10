"""Сид системных сплитов идемпотентен и не трогает существующие id (P1-03 ч.1)."""
import pytest
from sqlalchemy import select

from api.seed_splits import ensure_system_splits
from api.services.models import SplitBlueprint
from api.services.structure.split_catalog import SPLITS
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _system_splits() -> dict[str, str]:
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(SplitBlueprint).where(SplitBlueprint.is_system.is_(True))
        )).scalars().all()
    return {row.name: str(row.id) for row in rows}


async def test_seed_fills_the_whole_catalog_and_repeats_without_duplicates():
    async with SessionLocal() as db:
        await ensure_system_splits(db)
        await db.commit()

    after_first = await _system_splits()
    assert {s.name for s in SPLITS} <= set(after_first)

    async with SessionLocal() as db:
        created = await ensure_system_splits(db)
        await db.commit()

    assert created == 0
    assert await _system_splits() == after_first
