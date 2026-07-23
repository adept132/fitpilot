"""Срез истории по упражнению для относительных правил anomaly_guard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.anomaly_guard import ExerciseStats
from api.services.models import (
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)

# Окно наблюдения [КОНФИГ].
HISTORY_DAYS = 90


async def load_exercise_stats(
    db: AsyncSession, app_user_id: int, exercise_id: int
) -> ExerciseStats:
    """Считает историю по рабочим завершённым подходам за 90 дней.

    Уже помеченные аномалии исключаются: иначе одна ошибка расширила бы
    коридор для следующих.
    """
    since = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)

    base = (
        select(WorkoutSessionSet.weight, WorkoutSessionSet.reps, WorkoutSession.id)
        .select_from(WorkoutSessionSet)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .join(
            WorkoutSession,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSessionExercise.exercise_id == exercise_id,
            WorkoutSession.started_at >= since,
            WorkoutSessionSet.is_completed.is_(True),
            WorkoutSessionSet.is_anomalous.is_(False),
            WorkoutSessionSet.set_type == "normal",
            WorkoutSessionSet.weight.isnot(None),
            WorkoutSessionSet.reps.isnot(None),
        )
    )

    rows = (await db.execute(base)).all()
    if not rows:
        return ExerciseStats(sessions=0, best_e1rm=None, median_weight_kg=None)

    weights = sorted(float(r[0]) for r in rows)
    session_ids = {r[2] for r in rows}
    best_e1rm = max(float(r[0]) * (1 + int(r[1]) / 30) for r in rows)

    mid = len(weights) // 2
    median = (
        weights[mid]
        if len(weights) % 2 == 1
        else (weights[mid - 1] + weights[mid]) / 2
    )

    return ExerciseStats(
        sessions=len(session_ids),
        best_e1rm=best_e1rm,
        median_weight_kg=median,
    )
