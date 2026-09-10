"""Пересчёт личных рекордов по сырым подходам (P1-14).

Отдельно от progression.repository намеренно: та функция строит
состояние движка из ExerciseHistory — окна последних HISTORY_LIMIT=12
сессий. Для прогрессии окна достаточно, для рекордов — нет.
"""

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import (
    UserExerciseProgressionState,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.progression.records import SetInput, fold_records


async def rebuild_records(
    session: AsyncSession,
    app_user_id: int,
    exercise_ids: Iterable[int],
) -> None:
    """Пересчитать колонку records для указанных упражнений.

    Фильтр допуска дословно совпадает с /progress/achievements: иначе
    живая плашка и хронология достижений разошлись бы.
    """
    ids = [int(x) for x in exercise_ids]
    if not ids:
        return

    rows = (await session.execute(
        select(
            WorkoutSessionExercise.exercise_id,
            WorkoutSessionSet.weight,
            WorkoutSessionSet.reps,
            WorkoutSession.finished_at,
            WorkoutSession.id,
        )
        .select_from(WorkoutSessionSet)
        .join(WorkoutSessionExercise)
        .join(WorkoutSession)
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "finished",
            WorkoutSession.finished_at.is_not(None),
            WorkoutSessionExercise.exercise_id.in_(ids),
            WorkoutSessionSet.is_completed.is_(True),
            WorkoutSessionSet.is_anomalous.is_(False),
            WorkoutSessionSet.set_type == "normal",
            WorkoutSessionSet.weight.is_not(None),
            WorkoutSessionSet.reps.is_not(None),
            WorkoutSessionSet.weight > 0,
            WorkoutSessionSet.reps > 0,
        )
    )).all()

    by_exercise: dict[int, list[SetInput]] = {ex_id: [] for ex_id in ids}
    for exercise_id, weight, reps, finished_at, workout_id in rows:
        by_exercise[exercise_id].append(SetInput(
            weight=float(weight),
            reps=int(reps),
            at=finished_at,
            workout_id=workout_id,
        ))

    existing = {
        row.exercise_id: row
        for row in (await session.execute(
            select(UserExerciseProgressionState).where(
                UserExerciseProgressionState.app_user_id == app_user_id,
                UserExerciseProgressionState.exercise_id.in_(ids),
            )
        )).scalars().all()
    }

    for exercise_id, sets in by_exercise.items():
        row = existing.get(exercise_id)
        if row is None:
            row = UserExerciseProgressionState(
                app_user_id=app_user_id, exercise_id=exercise_id,
            )
            session.add(row)
        records = fold_records(sets)
        # datetime не сериализуется в JSONB как есть.
        row.records = _isoformat_dates(records)


def _isoformat_dates(records: dict) -> dict:
    def conv(entry: dict | None) -> dict | None:
        if entry is None:
            return None
        out = dict(entry)
        at = out.get("at")
        if at is not None and not isinstance(at, str):
            out["at"] = at.isoformat()
        return out

    return {
        "weight_at_reps": {k: conv(v) for k, v in records["weight_at_reps"].items()},
        "band": {k: conv(v) for k, v in records["band"].items()},
        "set_volume": conv(records["set_volume"]),
    }
