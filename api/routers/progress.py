from datetime import timezone, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.exercises import ExerciseFullHistoryResponse
from api.schemas.progress import FatigueWeekData, FatigueArchitectureResponse
from api.services.app_user_service import get_current_app_user
from api.services.exercise_search_service import ExerciseSearchService
from api.services.models import Exercise, WorkoutSessionSet, WorkoutSessionExercise, AppUserProfile, WorkoutSession
from api.services.statistics_service import get_weekly_performed_sets

router = APIRouter()


@router.get("/api/progress/volume-overview")
async def get_volume_overview(
        current_user=Depends(get_current_app_user),  # Переименовали для ясности, так как прилетает объект AppUser
        db: AsyncSession = Depends(get_db)
):
    # Достаем настоящий числовой ID из объекта
    actual_user_id = current_user.id

    # 1. Запрос профиля с правильным ID
    query = select(AppUserProfile).where(AppUserProfile.app_user_id == actual_user_id)
    result = await db.execute(query)
    user_profile = result.scalar_one_or_none()

    if not user_profile:
        raise HTTPException(status_code=404, detail="Профиль пользователя не найден")

    # Достаем JSONB с бюджетом
    current_budget = user_profile.volume_budget

    # 2. Считаем выполненные подходы за неделю (передаем только числовой ID!)
    performed_sets = await get_weekly_performed_sets(db, actual_user_id)

    # 3. Отправляем готовую склейку
    return {
        "budget": current_budget,
        "performed_sets": performed_sets
    }


@router.get("/progress/exercise-history/{exercise_id}", response_model=ExerciseFullHistoryResponse)
async def get_exercise_history(
        exercise_id: int,
        session: AsyncSession = Depends(get_db),
        current_user=Depends(get_current_app_user),  # Изменяем имя переменной для безопасности
):
    # Гарантированно вытаскиваем числовой ID из объекта пользователя
    actual_user_id = current_user.id if hasattr(current_user, "id") else current_user

    # Передаем уже очищенный actual_user_id в метод сервиса
    history_data = await ExerciseSearchService.get_exercise_analytics_history(
        session=session,
        user_id=actual_user_id,  # Исправлено здесь
        exercise_id=exercise_id
    )

    if not history_data:
        raise HTTPException(status_code=404, detail="Упражнение не найдено или по нему нет записей")

    return history_data


@router.get("/progress/fatigue/{muscle_group}", response_model=FatigueArchitectureResponse)
async def get_fatigue_architecture(
        muscle_group: str,
        weeks: int = 4,  # По умолчанию берем срез за месяц
        current_user=Depends(get_current_app_user),
        db: AsyncSession = Depends(get_db)
):
    actual_user_id = current_user.id

    # Определяем точку отсчета
    start_date = datetime.now(timezone.utc) - timedelta(weeks=weeks)

    # Условия:
    # 1. Прямой объем: целевая мышца является главной (main_muscle_group)
    is_direct = Exercise.main_muscle_group == muscle_group

    # 2. Косвенный объем: целевая мышца лежит внутри JSONB массива (secondary_muscle_groups)
    # В PostgreSQL для JSONB массивов отлично работает метод .contains()
    is_indirect = Exercise.secondary_muscle_groups.contains([muscle_group])

    stmt = (
        select(
            func.date_trunc('week', WorkoutSession.started_at).label('week_start'),
            func.sum(case((is_direct, 1), else_=0)).label('direct_sets'),
            func.sum(case((is_indirect, 1), else_=0)).label('indirect_sets')
        )
        .select_from(WorkoutSessionSet)
        .join(WorkoutSessionExercise, WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id)
        .join(WorkoutSession, WorkoutSessionExercise.workout_session_id == WorkoutSession.id)
        .join(Exercise, WorkoutSessionExercise.exercise_id == Exercise.id)
        .where(
            WorkoutSession.app_user_id == actual_user_id,
            WorkoutSession.status == 'finished',
            WorkoutSessionSet.is_completed == True,
            WorkoutSession.started_at >= start_date,
            (is_direct | is_indirect)  # Берем только если мышца вообще участвовала
        )
        .group_by('week_start')
        .order_by('week_start')
    )

    result = await db.execute(stmt)
    rows = result.all()

    history = []
    for row in rows:
        # Превращаем объект даты в строку для фронтенда
        week_str = row.week_start.strftime("%Y-%m-%d") if row.week_start else ""
        direct_count = float(row.direct_sets or 0)
        indirect_count = float(row.indirect_sets or 0)

        history.append(FatigueWeekData(
            week_start=week_str,
            direct_volume=direct_count * 1.0,  # Прямой объем считаем как 1 сет = 1
            indirect_volume=indirect_count * 0.5  # Косвенный режем пополам (1 сет = 0.5)
        ))

    return FatigueArchitectureResponse(
        muscle_group=muscle_group,
        history=history
    )