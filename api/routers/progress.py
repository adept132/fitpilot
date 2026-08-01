from datetime import timezone, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.exercises import ExerciseFullHistoryResponse
from api.schemas.progress import (
    ExerciseForecastResponse,
    FatigueWeekData,
    FatigueArchitectureResponse,
    ReadinessResponse,
    ReadinessBand,
    ProgressionResponse,
    DataQualityResponse,
    DisciplineResponse,
    DisciplineDay,
    DisciplineDensity,
)
from api.services.app_user_service import get_current_app_user
from api.services.exercise_search_service import ExerciseSearchService
from api.services.fatigue.service import compute_readiness
from api.services.forecast_service import build_strength_forecast
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


@router.get("/progress/exercise-forecast/{exercise_id}", response_model=ExerciseForecastResponse)
async def get_exercise_forecast(
        exercise_id: int,
        session: AsyncSession = Depends(get_db),
        current_user=Depends(get_current_app_user),
):
    """Прогноз e1RM по упражнению: текущий уровень, темп роста, потолок и точки
    прогнозной линии. Интерактивный «что-если» клиент считает локально."""
    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id)
    )).scalar_one_or_none()

    forecast = await build_strength_forecast(
        session=session,
        user_id=current_user.id,
        exercise_id=exercise_id,
        experience_level=profile.experience_level if profile else None,
        settings=profile.settings if profile else None,
    )
    if forecast is None:
        raise HTTPException(status_code=404, detail="Упражнение не найдено")

    return ExerciseForecastResponse(**forecast)


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


@router.get("/progress/readiness", response_model=ReadinessResponse)
async def get_readiness(
        current_user=Depends(get_current_app_user),
        db: AsyncSession = Depends(get_db),
):
    """Относительная готовность по отсекам и темп роста нагрузки.

    Абсолютных процентов усталости не отдаём: только z-оценка к собственной
    истории и полоса. При недостатке истории confidence = cold_start, и z
    не отдаётся вовсе — без истории это шум.
    """
    report = await compute_readiness(db, current_user.id)

    def band(r) -> ReadinessBand:
        return ReadinessBand(
            z=r.z,
            band=r.band,
            recovery_hours=r.recovery_hours,
            days_since_load=r.days_since_load,
        )

    return ReadinessResponse(
        model_version=report.model_version,
        computed_at=report.computed_at,
        confidence=report.confidence,
        systemic=band(report.systemic),
        muscular={m: band(r) for m, r in report.muscular.items()},
        mechanical=band(report.mechanical),
        progression=ProgressionResponse(
            ratio=report.progression.ratio,
            wow_change_pct=report.progression.wow_change_pct,
            chronic_level=report.progression.chronic_level,
            flag=report.progression.flag,
        ),
        data_quality=DataQualityResponse(
            effort_labeled_pct=report.effort_labeled_pct,
            imported_pct=report.imported_pct,
        ),
    )


# Границы правдоподобной длительности сессии [КОНФИГ]: забытая незавершённой
# тренировка дала бы длительность в сутки и обнулила бы плотность, а импорт
# из CSV обычно вообще без finished_at.
MIN_SESSION_MIN = 10
MAX_SESSION_MIN = 300
# [КОНФИГ] Минимум сессий, отвечающих гвардам, ниже которого плотность не отдаём
# вовсе, а не подсовываем клиенту число, посчитанное по одной случайной тренировке.
MIN_SESSIONS_FOR_DENSITY = 3
# [КОНФИГ] Окно расчёта плотности — всегда 28 дней, независимо от запрошенного
# окна календаря. Метрики так и называются (*_28d).
DENSITY_WINDOW_DAYS = 28
# [КОНФИГ] Верхний предел запрашиваемого окна календаря (недель), чтобы выборка
# не разрасталась безгранично при клиентском weeks.
MAX_DISCIPLINE_WEEKS = 53


@router.get("/progress/discipline", response_model=DisciplineResponse)
async def get_discipline(
        weeks: int = 13,
        current_user=Depends(get_current_app_user),
        db: AsyncSession = Depends(get_db),
):
    """Календарь дисциплины и плотность тренировок из реальных подходов."""
    now = datetime.now(timezone.utc)
    # weeks приходит от клиента — клампим, чтобы since не ушёл в будущее (<=0)
    # и выборка не разрослась безгранично.
    weeks = max(1, min(weeks, MAX_DISCIPLINE_WEEKS))
    days_count = weeks * 7
    since = now - timedelta(days=days_count - 1)

    # Выравниваем начало окна на понедельник. Клиент рисует heatmap колонками по
    # неделям (понедельник сверху), и если окно начинается с середины недели, в
    # первой колонке остаются прозрачные дыры — визуально «оторванный» квадрат.
    # Сдвигаем назад, а не вперёд: так окно только расширяется и данные не теряются.
    lead_days = since.weekday()  # Monday = 0
    if lead_days:
        since -= timedelta(days=lead_days)
        days_count += lead_days

    # Плотность считается всегда за фиксированные 28 дней. Если запрошенное окно
    # календаря короче (weeks < 4), гоним запрос по более раннему из двух краёв —
    # иначе сессии между `since` и 28 днями назад не попали бы в выборку, и
    # «28-дневная» плотность молча посчиталась бы по неполному периоду.
    density_window_start = now - timedelta(days=DENSITY_WINDOW_DAYS)
    query_since = min(since, density_window_start)

    rows = (await db.execute(
        select(
            WorkoutSession.id,
            WorkoutSession.started_at,
            WorkoutSession.finished_at,
            WorkoutSessionSet.weight,
            WorkoutSessionSet.reps,
        )
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
            WorkoutSession.app_user_id == current_user.id,
            WorkoutSession.started_at >= query_since,
            WorkoutSessionSet.is_completed.is_(True),
            WorkoutSessionSet.set_type.in_(["normal", "drop"]),
            WorkoutSessionSet.is_anomalous.is_(False),
        )
    )).all()

    # Дни идут подряд, включая пустые: иначе на клиенте не построить сетку.
    # Строим ровно запрошенное окно календаря (days_count), а не окно выборки.
    buckets: dict[str, dict] = {}
    for offset in range(days_count):
        key = (since + timedelta(days=offset)).strftime("%Y-%m-%d")
        buckets[key] = {"sets": 0, "sessions": set(), "volume_kg": 0.0}

    density_sets = 0
    # id сессии -> её длительность в минутах: длительность каждой сессии
    # должна попасть в знаменатель ровно один раз, а не по разу на подход.
    density_sessions: dict[int, float] = {}

    for row in rows:
        key = row.started_at.strftime("%Y-%m-%d")
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["sets"] += 1
        bucket["sessions"].add(row.id)
        bucket["volume_kg"] += float(row.weight or 0) * int(row.reps or 0)

        if row.started_at >= density_window_start and row.finished_at:
            minutes = (row.finished_at - row.started_at).total_seconds() / 60.0
            if MIN_SESSION_MIN <= minutes <= MAX_SESSION_MIN:
                density_sets += 1
                if row.id not in density_sessions:
                    density_sessions[row.id] = minutes

    density_minutes = sum(density_sessions.values())
    sets_per_hour = None
    median_duration = None
    if len(density_sessions) >= MIN_SESSIONS_FOR_DENSITY and density_minutes > 0:
        sets_per_hour = round(density_sets / (density_minutes / 60.0), 1)
        durations = sorted(density_sessions.values())
        mid = len(durations) // 2
        median_duration = round(
            durations[mid]
            if len(durations) % 2 == 1
            else (durations[mid - 1] + durations[mid]) / 2,
            1,
        )

    return DisciplineResponse(
        weeks=weeks,
        days=[
            DisciplineDay(
                date=key,
                sets=v["sets"],
                sessions=len(v["sessions"]),
                volume_kg=round(v["volume_kg"], 1),
            )
            for key, v in buckets.items()
        ],
        density=DisciplineDensity(
            sets_per_hour_28d=sets_per_hour,
            sessions_28d=len(density_sessions),
            median_duration_min=median_duration,
        ),
    )