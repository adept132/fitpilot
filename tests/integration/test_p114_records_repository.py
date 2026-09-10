"""P1-14: пересчёт рекордов по сырым подходам.

Отдельно от tests/test_progression_records.py (чистые правила без БД):
здесь проверяется ровно то, ради чего rebuild_records не переиспользует
rebuild_state — независимость от окна HISTORY_LIMIT.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import (
    AppUser,
    Exercise,
    UserExerciseProgressionState,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.progression.records_repository import rebuild_records


async def _finished_workout(
    db: AsyncSession,
    user: AppUser,
    exercise: Exercise,
    *,
    days_ago: int,
    weight: float,
    reps: int,
    set_type: str = "normal",
    is_anomalous: bool = False,
) -> None:
    """Одна завершённая тренировка с одним подходом. Прямой записью в БД —
    движок здесь не участвует, проверяется только выборка рекордов."""
    workout = WorkoutSession(
        app_user_id=user.id,
        source="free",
        status="finished",
        finished_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(workout)
    await db.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id,
        exercise_id=exercise.id,
        order_index=0,
    )
    db.add(se)
    await db.flush()

    db.add(WorkoutSessionSet(
        workout_session_exercise_id=se.id,
        set_number=1,
        set_type=set_type,
        weight=weight,
        reps=reps,
        effort_level="medium",
        is_completed=True,
        is_anomalous=is_anomalous,
    ))
    await db.commit()


async def _records_of(db: AsyncSession, exercise_id: int) -> dict | None:
    row = (await db.execute(
        select(UserExerciseProgressionState).where(
            UserExerciseProgressionState.exercise_id == exercise_id,
        )
    )).scalar_one_or_none()
    return None if row is None else row.records


@pytest.mark.asyncio
async def test_rebuild_records_sees_beyond_history_window(
    db, test_user, fresh_exercise,
):
    """Рекорд за пределами окна HISTORY_LIMIT=12 сессий не теряется.

    Рекорд поставлен в самой старой тренировке; ещё 14 тренировок сверху
    вытесняют её из окна, по которому работает rebuild_state.
    """
    await _finished_workout(db, test_user, fresh_exercise, days_ago=100, weight=100.0, reps=5)
    for day in range(1, 15):
        await _finished_workout(db, test_user, fresh_exercise, days_ago=day, weight=60.0, reps=5)

    await rebuild_records(db, test_user.id, [fresh_exercise.id])
    await db.commit()

    records = await _records_of(db, fresh_exercise.id)
    assert records["weight_at_reps"]["5"]["weight"] == 100.0


@pytest.mark.asyncio
async def test_rebuild_records_applies_admission_filter(
    db, test_user, fresh_exercise,
):
    """Разминка, drop и аномалия рекордов не дают."""
    await _finished_workout(db, test_user, fresh_exercise, days_ago=4, weight=60.0, reps=5)
    await _finished_workout(db, test_user, fresh_exercise, days_ago=3, weight=200.0, reps=5, set_type="warmup")
    await _finished_workout(db, test_user, fresh_exercise, days_ago=2, weight=300.0, reps=5, set_type="drop")
    await _finished_workout(db, test_user, fresh_exercise, days_ago=1, weight=400.0, reps=5, is_anomalous=True)

    await rebuild_records(db, test_user.id, [fresh_exercise.id])
    await db.commit()

    records = await _records_of(db, fresh_exercise.id)
    assert records["weight_at_reps"]["5"]["weight"] == 60.0


@pytest.mark.asyncio
async def test_rebuild_records_creates_state_row_when_absent(
    db, test_user, fresh_exercise,
):
    """fresh_exercise истории не имеет — строки состояния тоже нет."""
    await _finished_workout(db, test_user, fresh_exercise, days_ago=1, weight=60.0, reps=5)

    await rebuild_records(db, test_user.id, [fresh_exercise.id])
    await db.commit()

    records = await _records_of(db, fresh_exercise.id)
    assert records is not None
    assert records["set_volume"]["value"] == 300.0
