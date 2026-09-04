"""Apply and verify the reviewed system-exercise localization catalog."""
from __future__ import annotations

import asyncio

from scripts import backfill_exercise_localizations as backfill


async def run_gate() -> int:
    applied = await backfill._run(True, backfill.DEFAULT_TRANSLATIONS)
    if applied != 0:
        return applied
    return await backfill._run(False, backfill.DEFAULT_TRANSLATIONS)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_gate()))
