from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import Exercise, WorkoutSessionSet, WorkoutSessionExercise, WorkoutSession


async def get_weekly_performed_sets(db: AsyncSession, user_id: int) -> dict:
    # Определяем начало текущей недели (понедельник 00:00:00)
    today = datetime.utcnow()
    start_of_week = today - timedelta(days=today.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)

    query = (
        select(
            Exercise.main_muscle_group,
            func.count(WorkoutSessionSet.id).label('total_sets')
        )
        # Выстраиваем цепочку джойнов от Сета -> к Упражнению в сессии -> к Справочнику упражнений
        .join(WorkoutSessionExercise, WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id)
        .join(Exercise, WorkoutSessionExercise.exercise_id == Exercise.id)
        # Джойним саму сессию, чтобы отфильтровать по юзеру
        .join(WorkoutSession, WorkoutSessionExercise.workout_session_id == WorkoutSession.id)
        .where(
            WorkoutSession.app_user_id == user_id,
            WorkoutSessionSet.is_completed == True,
            # Важнейший фильтр: считаем только рабочие подходы и дропсеты, игнорируем разминку
            WorkoutSessionSet.set_type.in_(["normal", "drop"]),
            WorkoutSessionSet.updated_at >= start_of_week
        )
        .group_by(Exercise.main_muscle_group)
    )

    result = await db.execute(query)

    # Конвертируем результат в словарь для фронтенда: {"chest": 12, "triceps": 14}
    performed_sets = {row.main_muscle_group: row.total_sets for row in result.all()}

    return performed_sets