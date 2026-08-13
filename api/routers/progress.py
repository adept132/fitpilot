from datetime import timezone, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
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
    ProgressAchievement,
)
from api.schemas.volume import (
    AdherenceRead,
    MuscleVolumeRead,
    VolumeOverviewRead,
    WindowRead,
)
from api.services.app_user_service import get_current_app_user
from api.services.exercise_search_service import ExerciseSearchService
from api.services.fatigue.service import compute_readiness
from api.services.forecast_service import build_strength_forecast
from api.services.models import Exercise, WorkoutSessionSet, WorkoutSessionExercise, AppUserProfile, WorkoutSession
from api.services.volume import repository as volume_repo
from api.services.volume.landmarks import landmarks_for, reachable_mrv
from api.services.volume.service import targets_from_budget

router = APIRouter()


@router.get("/progress/achievements", response_model=list[ProgressAchievement])
async def get_progress_achievements(
        limit: int = Query(100, ge=1, le=500),
        current_user=Depends(get_current_app_user),
        db: AsyncSession = Depends(get_db),
):
    """Immutable chronology of e1RM records using the Brzycki formula.

    The first valid performance establishes the first record. Warm-ups, drop
    sets and anomalous values do not create records.
    """
    result = await db.execute(
        select(
            WorkoutSession.id,
            WorkoutSession.finished_at,
            WorkoutSessionExercise.exercise_id,
            Exercise.name,
            WorkoutSessionSet.weight,
            WorkoutSessionSet.reps,
        )
        .select_from(WorkoutSessionSet)
        .join(WorkoutSessionExercise)
        .join(WorkoutSession)
        .join(Exercise)
        .where(
            WorkoutSession.app_user_id == current_user.id,
            WorkoutSession.status == "finished",
            WorkoutSession.finished_at.is_not(None),
            WorkoutSessionSet.is_completed.is_(True),
            WorkoutSessionSet.is_anomalous.is_(False),
            WorkoutSessionSet.set_type == "normal",
            WorkoutSessionSet.weight.is_not(None),
            WorkoutSessionSet.reps.is_not(None),
            WorkoutSessionSet.weight > 0,
            WorkoutSessionSet.reps > 0,
        )
        .order_by(WorkoutSession.finished_at, WorkoutSession.id, WorkoutSessionExercise.exercise_id)
    )

    performances: dict[tuple[int, int], dict] = {}
    for workout_id, finished_at, exercise_id, exercise_name, raw_weight, raw_reps in result.all():
        key = (workout_id, exercise_id)
        item = performances.setdefault(key, {
            "workout_id": workout_id,
            "achieved_at": finished_at,
            "exercise_id": exercise_id,
            "exercise_name": exercise_name,
            "e1rm": 0.0,
            "weight": 0.0,
            "reps": 0,
        })
        weight, reps = float(raw_weight), int(raw_reps)
        # The formula is undefined at 37 reps and negative above it. Those
        # high-rep sets stay in workout history but cannot establish e1RM.
        if reps >= 37:
            continue
        e1rm = weight * 36.0 / (37.0 - reps)
        if e1rm > item["e1rm"]:
            item["e1rm"] = e1rm
            item["weight"] = weight
            item["reps"] = reps

    best: dict[int, float] = {}
    achievements: list[ProgressAchievement] = []
    for item in performances.values():
        if item["e1rm"] <= 0:
            continue
        previous = best.get(item["exercise_id"])
        if previous is None or item["e1rm"] > previous + 1e-6:
            achievements.append(ProgressAchievement(
                id=f'{item["workout_id"]}:{item["exercise_id"]}:e1rm',
                exercise_id=item["exercise_id"],
                exercise_name=item["exercise_name"],
                e1rm=round(item["e1rm"], 1),
                previous_e1rm=round(previous, 1) if previous is not None else None,
                weight=round(item["weight"], 2),
                reps=item["reps"],
                achieved_at=item["achieved_at"],
                workout_id=item["workout_id"],
            ))
        best[item["exercise_id"]] = max(previous or 0.0, item["e1rm"])

    achievements.sort(key=lambda item: (item.achieved_at, item.workout_id), reverse=True)
    return achievements[:limit]


@router.get("/api/progress/volume-overview", response_model=VolumeOverviewRead)
async def get_volume_overview(
        current_user=Depends(get_current_app_user),
        db: AsyncSession = Depends(get_db),
) -> VolumeOverviewRead:
    """Текущее окно объёма: цель, предписание, факт и прогноз по мышцам.

    Открытое окно считается живым запросом — оно меняется после каждого
    подхода, кэшировать нечего. Прогноз получается сложением уже
    сгенерированного плана, а не экстраполяцией.
    """
    today = volume_repo.utc_today()

    profile = (await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id)
    )).scalar_one_or_none()
    level = profile.experience_level if profile else None
    budget = profile.volume_budget if profile else None
    targets = targets_from_budget(profile)

    window = await volume_repo.current_window(db, current_user.id, today)
    if window is None:
        return VolumeOverviewRead(level=level, budget=budget)

    prescribed = await volume_repo.prescribed_for(db, current_user.id, window)
    performed = await volume_repo.performed_for(db, current_user.id, window)
    planned_work_sets, completed_work_sets = await volume_repo.physical_set_totals(
        db, current_user.id, window
    )
    rows = volume_repo.build_rows(targets, prescribed, performed)
    adherence = await volume_repo.adherence_for_range(
        db, current_user.id, window.start_date, window.end_date
    )

    # Прогноз: факт на сегодня плюс предписание ОСТАВШИХСЯ дней окна.
    remaining = volume_repo.Window(
        block_id=window.block_id, window_index=window.window_index,
        phase_number=window.phase_number,
        start_date=max(today + timedelta(days=1), window.start_date),
        end_date=window.end_date, is_deload=window.is_deload,
    )
    remaining_prescribed = (
        await volume_repo.prescribed_for(db, current_user.id, remaining)
        if remaining.start_date <= remaining.end_date
        else {}
    )

    # Спека §5.1: показываемый прямой потолок клампится достижимым в
    # КОНКРЕТНОМ сплите. У человека, тренирующего грудь раз в неделю,
    # табличный потолок недосягаем и висел бы молчащим предупреждением.
    frequency = await volume_repo.muscle_frequency(db, current_user.id, window)

    muscles: dict[str, MuscleVolumeRead] = {}
    # P0-09 I4: shim совместимости со сборками ДО shape v2 (см. докстринг
    # поля в api/schemas/volume.py) — то же эффективное выполненное, что
    # уходит в muscles[*], просто сложенное в одно число на мышцу.
    performed_sets: dict[str, float] = {}
    for muscle, row in rows.items():
        lm = landmarks_for(muscle, level)
        if lm is None:
            continue
        ahead = remaining_prescribed.get(muscle)
        muscles[muscle] = MuscleVolumeRead(
            target=row.target,
            prescribed=row.prescribed,
            performed_direct=row.performed_direct,
            performed_indirect=row.performed_indirect,
            forecast=row.performed_effective + (ahead.effective if ahead else 0.0),
            mev=lm.mev, mav=lm.mav, mrv=lm.mrv,
            mev_direct=lm.mev_direct,
            mrv_direct=reachable_mrv(muscle, level, frequency.get(muscle, 1)),
        )
        performed_sets[muscle] = row.performed_effective

    length = (window.end_date - window.start_date).days + 1
    return VolumeOverviewRead(
        window=WindowRead(
            index=window.window_index,
            day=min(length, (today - window.start_date).days + 1),
            length=length,
            start_date=window.start_date.isoformat(),
            end_date=window.end_date.isoformat(),
            phase_number=window.phase_number,
            is_deload=window.is_deload,
        ),
        level=level,
        adherence=AdherenceRead(
            planned_days=adherence.planned_days,
            completed_days=adherence.completed_days,
            missed_days=adherence.missed_days,
        ),
        planned_work_sets=planned_work_sets,
        completed_work_sets=completed_work_sets,
        muscles=muscles,
        budget=budget,
        performed_sets=performed_sets,
    )


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
