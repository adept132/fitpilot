"""Apply and verify the reviewed system-exercise localization catalog."""
from __future__ import annotations

import asyncio
from pathlib import Path

from scripts import backfill_exercise_localizations as backfill


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SYSTEM_IDS = frozenset(range(76, 270))


def _matches_expected_ids(label: str, actual_ids: set[int]) -> bool:
    missing_ids = sorted(EXPECTED_SYSTEM_IDS - actual_ids)
    extra_ids = sorted(actual_ids - EXPECTED_SYSTEM_IDS)
    if not missing_ids and not extra_ids:
        return True
    if missing_ids:
        print(f"{label} missing IDs: " + ", ".join(map(str, missing_ids)))
    if extra_ids:
        print(f"{label} unexpected IDs: " + ", ".join(map(str, extra_ids)))
    return False


async def _default_exercise_ids() -> set[int]:
    async with backfill.SessionLocal() as session:
        result = await session.execute(
            backfill.select(backfill.Exercise.id).where(
                backfill.Exercise.source == "default"
            )
        )
    return set(result.scalars())


async def run_gate() -> int:
    catalog_ids = set(backfill.load_translations(backfill.DEFAULT_TRANSLATIONS))
    if not _matches_expected_ids("catalog", catalog_ids):
        return 1

    database_ids = await _default_exercise_ids()
    if not _matches_expected_ids("database default exercises", database_ids):
        return 1

    applied = await backfill._run(True, backfill.DEFAULT_TRANSLATIONS)
    if applied != 0:
        return applied
    return await backfill._run(False, backfill.DEFAULT_TRANSLATIONS)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_gate()))
