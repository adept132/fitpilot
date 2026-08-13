from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from api.routers.progress import get_progress_achievements


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_first_performance_and_second_record_are_both_returned():
    at = datetime(2026, 8, 12, tzinfo=timezone.utc)
    rows = [
        (1, at, 7, "Bench press", 80, 8),
        (2, at, 7, "Bench press", 85, 8),
    ]

    items = await get_progress_achievements(
        limit=8,
        current_user=SimpleNamespace(id=42),
        db=_Db(rows),
    )

    assert len(items) == 2
    assert items[0].workout_id == 2
    assert items[0].e1rm == pytest.approx(105.5, abs=0.1)
    assert items[0].previous_e1rm == pytest.approx(99.3, abs=0.1)
    assert items[0].weight == 85
    assert items[0].reps == 8


@pytest.mark.asyncio
async def test_first_performance_creates_initial_record():
    at = datetime(2026, 8, 12, tzinfo=timezone.utc)
    items = await get_progress_achievements(
        limit=8,
        current_user=SimpleNamespace(id=42),
        db=_Db([(1, at, 7, "Bench press", 80, 8)]),
    )
    assert len(items) == 1
    assert items[0].previous_e1rm is None


@pytest.mark.asyncio
async def test_records_are_chronological_and_same_exercise_can_repeat():
    at = datetime(2026, 8, 12, tzinfo=timezone.utc)
    later = datetime(2026, 8, 13, tzinfo=timezone.utc)
    rows = [
        (1, at, 7, "Bench press", 80, 8),
        (2, at, 7, "Bench press", 85, 8),
        (3, later, 7, "Bench press", 90, 8),
    ]
    items = await get_progress_achievements(limit=8, current_user=SimpleNamespace(id=42), db=_Db(rows))
    assert [item.workout_id for item in items] == [3, 2, 1]
