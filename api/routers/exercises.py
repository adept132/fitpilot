from typing import Optional, List

from sqlalchemy import func, select, case, desc, update
from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_db
from api.schemas.workouts import ExerciseShortResponse
from api.services.app_user_service import get_current_app_user
from api.services.exercise_search_service import ExerciseSearchService
from api.services.exercise_utils import get_base_exercise_query
from api.services.fatigue_tiers import calculate_fatigue_tier
from api.services.heuristics import HeuristicsEngine
from app.database import get_session
from api.services.models import Exercise, WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet, AppUser
from api.schemas.exercises import (
    ExerciseListItemResponse,
    ExerciseDetailResponse,
    ExerciseHistoryItemResponse,
    ExerciseHistoryWorkoutDetailResponse,
    ExerciseHistoryWorkoutSetResponse,
    ExerciseLastPerformanceResponse, ExerciseSearchItem, MuscleGroupItem, LastWorkoutResponse,
    ExerciseAlternativeResponse, ReplaceExerciseRequest, CustomExerciseCreate,
)

router = APIRouter(tags=["exercises"])


@router.get("/exercises", response_model=list[ExerciseListItemResponse])
async def list_exercises(
        q: str | None = Query(default=None),
        type: str | None = Query(default=None),
        equipment: str | None = Query(default=None),
        recent: bool = Query(default=False),
        source: Optional[str] = Query(None), # <--- Принимаем
        session: AsyncSession = Depends(get_db),
        current_user=Depends(get_current_app_user),
):
    actual_user_id = current_user.id if hasattr(current_user, "id") else current_user

    # 1. ПЕРЕДАЕМ source В СЕРВИС:
    items = await ExerciseSearchService.search_exercises(
        session=session,
        user_id=actual_user_id,
        q=q,
        type=type,
        equipment=equipment,
        recent=recent,
        source=source # <--- Передали!
    )

    response_items = []

    for item in items:
        if isinstance(item, dict):
            response_items.append(
                ExerciseListItemResponse(
                    id=item.get("id"),
                    name=item.get("name"),
                    category=item.get("category") or "base",
                    main_muscle_group=item.get("main_muscle_group") or "unknown",
                    secondary_muscle_groups=item.get("secondary_muscle_groups") or [],
                    difficulty=item.get("difficulty") or "beginner",
                    equipment_needed=item.get("equipment_needed") or [],
                    fatigue_tier=item.get("fatigue_tier") or 2,
                    # 2. ВОЗВРАЩАЕМ source ИЗ СЛОВАРЯ
                    source=item.get("source") or "default",
                )
            )
        else:
            response_items.append(
                ExerciseListItemResponse(
                    id=item.id,
                    name=item.name,
                    category=item.category,
                    main_muscle_group=item.main_muscle_group,
                    secondary_muscle_groups=item.secondary_muscle_groups or [],
                    difficulty=item.difficulty,
                    equipment_needed=item.equipment_needed or [],
                    fatigue_tier=item.fatigue_tier,
                    # 2. ВОЗВРАЩАЕМ source ИЗ ОБЪЕКТА БД
                    source=item.source,
                )
            )

    return response_items

@router.get("/exercises/{exercise_id}", response_model=ExerciseDetailResponse)
async def get_exercise_detail(
    exercise_id: int,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    # Используем безопасный базовый запрос
    stmt = get_base_exercise_query(app_user.id).where(Exercise.id == exercise_id)
    result = await session.execute(stmt)
    exercise = result.scalar_one_or_none()

    if exercise is None:
        raise HTTPException(status_code=404, detail="Exercise not found")

    return ExerciseDetailResponse(
        id=exercise.id,
        name=exercise.name,
        category=exercise.category,
        main_muscle_group=exercise.main_muscle_group,
        secondary_muscle_groups=exercise.secondary_muscle_groups or [],
        equipment_needed=exercise.equipment_needed or [],
        difficulty=exercise.difficulty,
        description=exercise.description,
        source=exercise.source,
        video_url=exercise.video_url,
    )

@router.get(
    "/exercises/{exercise_id}/history",
    response_model=list[ExerciseHistoryItemResponse],
)
async def get_exercise_history(
    exercise_id: int,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    stmt = (
        select(
            WorkoutSession.id.label("workout_id"),
            WorkoutSession.finished_at,
            WorkoutSession.source,
            WorkoutSessionSet.weight,
            WorkoutSessionSet.reps,
            WorkoutSessionSet.is_completed,
        )
        .join(
            WorkoutSessionExercise,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .join(
            WorkoutSessionSet,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .where(
            WorkoutSession.app_user_id == app_user.id,
            WorkoutSession.status == "finished",
            WorkoutSessionExercise.exercise_id == exercise_id,
        )
        .order_by(WorkoutSession.finished_at.desc())
    )

    result = await session.execute(stmt)
    rows = result.all()

    grouped: dict[int, dict] = {}

    for row in rows:
        workout_id = row.workout_id

        if workout_id not in grouped:
            grouped[workout_id] = {
                "workout_id": workout_id,
                "finished_at": row.finished_at,
                "source": row.source,
                "sets_count": 0,
                "total_reps": 0,
                "total_volume": 0.0,
            }

        if row.is_completed:
            grouped[workout_id]["sets_count"] += 1
            grouped[workout_id]["total_reps"] += row.reps or 0
            grouped[workout_id]["total_volume"] += float((row.weight or 0) * (row.reps or 0))

    return [ExerciseHistoryItemResponse(**item) for item in grouped.values()]

@router.get(
    "/exercises/{exercise_id}/history/{workout_id}",
    response_model=ExerciseHistoryWorkoutDetailResponse,
)
async def get_exercise_history_workout_detail(
    exercise_id: int,
    workout_id: int,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.id == workout_id,
            WorkoutSession.app_user_id == app_user.id,
            WorkoutSession.status == "finished",
        )
        .options(
            selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.exercise),
            selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.sets),
        )
    )

    result = await session.execute(stmt)
    workout = result.scalar_one_or_none()

    if workout is None:
        raise HTTPException(status_code=404, detail="Workout not found")

    session_exercise = next(
        (item for item in workout.exercises if item.exercise_id == exercise_id),
        None,
    )

    if session_exercise is None:
        raise HTTPException(
            status_code=404,
            detail="Exercise not found in this workout",
        )

    completed_sets = [s for s in session_exercise.sets if s.is_completed]
    total_reps = sum(s.reps or 0 for s in completed_sets)
    total_volume = sum(float((s.weight or 0) * (s.reps or 0)) for s in completed_sets)

    return ExerciseHistoryWorkoutDetailResponse(
        workout_id=workout.id,
        finished_at=workout.finished_at,
        source=workout.source,
        exercise_id=session_exercise.exercise.id,
        exercise_name=session_exercise.exercise.name,
        sets_count=len(completed_sets),
        total_reps=total_reps,
        total_volume=total_volume,
        sets=[
            ExerciseHistoryWorkoutSetResponse(
                id=s.id,
                set_number=s.set_number,
                set_type=s.set_type,
                weight=float(s.weight) if s.weight is not None else None,
                reps=s.reps,
                notes=s.notes,
                is_completed=s.is_completed,
                effort_level=s.effort_level,
            )
            for s in session_exercise.sets
        ],
    )

@router.get(
    "/exercises/{exercise_id}/last-performance",
    response_model=Optional[ExerciseLastPerformanceResponse],
)
async def get_exercise_last_performance(
    exercise_id: int,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSession)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .where(
            WorkoutSession.app_user_id == app_user.id,
            WorkoutSession.status == "finished",
            WorkoutSessionExercise.exercise_id == exercise_id,
        )
        .options(
            selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.exercise),
            selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.sets),
        )
        .order_by(WorkoutSession.finished_at.desc())
    )

    result = await session.execute(stmt)
    workout = result.scalars().first()

    if workout is None:
        return None

    session_exercise = next(
        (item for item in workout.exercises if item.exercise_id == exercise_id),
        None,
    )

    if session_exercise is None:
        raise HTTPException(status_code=404, detail="Exercise not found in workout")

    completed_sets = [s for s in session_exercise.sets if s.is_completed]

    return ExerciseLastPerformanceResponse(
        workout_id=workout.id,
        finished_at=workout.finished_at,
        source=workout.source,
        exercise_id=session_exercise.exercise.id,
        exercise_name=session_exercise.exercise.name,
        sets=[
            ExerciseHistoryWorkoutSetResponse(
                id=s.id,
                set_number=s.set_number,
                set_type=s.set_type,
                weight=float(s.weight) if s.weight is not None else None,
                reps=s.reps,
                effort_level=s.effort_level,
                notes=s.notes,
                is_completed=s.is_completed,
            )
            for s in completed_sets
        ],
    )

@router.get("/search", response_model=list[ExerciseSearchItem])
async def search_exercises(
    q: Optional[str] = None,
    muscle_group: Optional[str] = None,
    type: Optional[str] = None,
    equipment: Optional[str] = None,
    recent: bool = False,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(get_current_app_user),
):
    return await ExerciseSearchService.search_exercises(
        session=session,
        user_id=user.id,
        q=q,
        muscle_group=muscle_group,
        type=type,
        equipment=equipment,
        recent=recent,
    )


# --- GROUPS ---

@router.get("/muscle-groups", response_model=list[MuscleGroupItem])
async def get_muscle_groups(
    type: Optional[str] = None,
    equipment: Optional[str] = None,
    recent: bool = False,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(get_current_app_user),
):
    return await ExerciseSearchService.get_muscle_groups(
        session=session,
        user_id=user.id,
        type=type,
        equipment=equipment,
        recent=recent,
    )


# --- LAST WORKOUT ---

@router.get("/last-for-context", response_model=LastWorkoutResponse)
async def get_last_for_context(
    session: AsyncSession = Depends(get_db),
    app_user=Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.app_user_id == app_user.id,
            WorkoutSession.status == "active",
        )
        .order_by(WorkoutSession.started_at.desc())
        .limit(1)
    )

    result = await session.execute(stmt)
    current_workout = result.scalar_one_or_none()

    if not current_workout:
        return {"exercises": []}

    exercises = await ExerciseSearchService.get_last_workout_for_context(
        session=session,
        user_id=app_user.id,
        current_workout=current_workout,
    )

    return {"exercises": exercises}

@router.get("/{exercise_id}/alternatives", response_model=List[ExerciseAlternativeResponse])
async def get_exercise_alternatives(
    exercise_id: int,
    db: AsyncSession = Depends(get_db)
):
    # 1. Находим исходное упражнение
    target_ex = await db.get(Exercise, exercise_id)
    if not target_ex:
        raise HTTPException(status_code=404, detail="Упражнение не найдено")

    # 2. Формируем логику начисления баллов (Scoring Model) прямо в SQL
    score_column = (
        case((Exercise.action == target_ex.action, 40), else_=0) +
        case((Exercise.vector == target_ex.vector, 20), else_=0) +
        case((Exercise.laterality == target_ex.laterality, 10), else_=0)
    ).label("match_score")

    # 3. Строим запрос:
    # - Жесткий фильтр по main_muscle_group
    # - Исключаем само исходное упражнение
    query = (
        select(Exercise, score_column)
        .where(
            Exercise.main_muscle_group == target_ex.main_muscle_group,
            Exercise.id != target_ex.id
        )
        .order_by(desc("match_score"), Exercise.name)
        .limit(15) # Ограничиваем выдачу топ-15 кандидатами
    )

    result = await db.execute(query)
    rows = result.all()

    # 4. Формируем ответ, склеивая объект Exercise и вычисленный score
    alternatives = []
    for ex_obj, score in rows:
        alt_data = ExerciseAlternativeResponse(
            id=ex_obj.id,
            name=ex_obj.name,
            main_muscle_group=ex_obj.main_muscle_group,
            equipment_needed=ex_obj.equipment_needed,
            match_score=score
        )
        alternatives.append(alt_data)

    return alternatives


@router.post("/sessions/{session_id}/exercises/{session_ex_id}/replace")
async def replace_session_exercise(
        session_id: int,
        session_ex_id: int,
        payload: ReplaceExerciseRequest,
        db: AsyncSession = Depends(get_db)
):
    # 1. Загружаем текущее упражнение в сессии вместе с его подходами
    query = (
        select(WorkoutSessionExercise)
        .where(
            WorkoutSessionExercise.id == session_ex_id,
            WorkoutSessionExercise.workout_session_id == session_id
        )
        .options(selectinload(WorkoutSessionExercise.sets))
    )
    result = await db.execute(query)
    target_session_ex = result.scalar_one_or_none()

    if not target_session_ex:
        raise HTTPException(status_code=404, detail="Упражнение в сессии не найдено")

    # 2. Проверяем, существует ли новое упражнение в БД
    new_ex = await db.get(Exercise, payload.new_exercise_id)
    if not new_ex:
        raise HTTPException(status_code=404, detail="Новое упражнение не найдено в БД")

    # === НОВЫЙ БЛОК: Защита от дубликатов ===
    duplicate_check = await db.execute(
        select(WorkoutSessionExercise).where(
            WorkoutSessionExercise.workout_session_id == session_id,
            WorkoutSessionExercise.exercise_id == payload.new_exercise_id
        )
    )
    if duplicate_check.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Это упражнение уже добавлено в текущую тренировку"
        )

    # 3. Анализируем подходы (Сортируем для порядка)
    all_sets = sorted(target_session_ex.sets, key=lambda s: s.set_number)
    completed_sets = [s for s in all_sets if s.is_completed]
    uncompleted_sets = [s for s in all_sets if not s.is_completed]

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # Блокируем замену ТОЛЬКО если подходы ЕСТЬ, и при этом они все выполнены.
    # Если подходов 0, all_sets = пустой список (False), и мы спокойно идем дальше.
    if all_sets and not uncompleted_sets:
        raise HTTPException(status_code=400, detail="Все подходы выполнены, заменять нечего")

    # ==========================================
    # СЦЕНАРИЙ 1: 0 выполненных подходов
    # ==========================================
    if not completed_sets:
        # Оптимизация: вместо физического удаления строки и создания новой,
        # мы просто подменяем ID упражнения. order_index и superset_group остаются нетронутыми!
        target_session_ex.exercise_id = payload.new_exercise_id

        # Сбрасываем вес/повторы в невыполненных сетах (новое упражнение = другие веса)
        for s in uncompleted_sets:
            s.weight = None
            s.reps = None

        await db.commit()
        return {"status": "replaced", "mode": "full_replace"}

    # ==========================================
    # СЦЕНАРИЙ 2: Разрезание упражнения
    # ==========================================
    else:
        # 1. Сдвигаем order_index у всех последующих упражнений на +1, чтобы освободить слот
        shift_query = (
            update(WorkoutSessionExercise)
            .where(
                WorkoutSessionExercise.workout_session_id == session_id,
                WorkoutSessionExercise.order_index > target_session_ex.order_index
            )
            .values(order_index=WorkoutSessionExercise.order_index + 1)
        )
        await db.execute(shift_query)

        # 2. Создаем новое упражнение прямо под старым
        new_session_ex = WorkoutSessionExercise(
            session_id=session_id,
            exercise_id=payload.new_exercise_id,
            order_index=target_session_ex.order_index + 1,
            superset_group=target_session_ex.superset_group,  # Копируем ID суперсета!
            notes=None
        )
        db.add(new_session_ex)
        await db.flush()  # Получаем ID нового session_exercise

        # 3. Переносим невыполненные сеты в новое упражнение
        # Важно: пересчитываем set_number с 1
        for idx, s in enumerate(uncompleted_sets, start=1):
            s.session_exercise_id = new_session_ex.id
            s.set_number = idx
            s.weight = None  # Очищаем историю веса старого упражнения
            s.reps = None

        await db.commit()
        return {"status": "split", "mode": "partial_transfer"}


@router.post("/exercises", response_model=ExerciseSearchItem, status_code=status.HTTP_201_CREATED)
async def create_custom_exercise(
        payload: CustomExerciseCreate,
        db: AsyncSession = Depends(get_db),
        current_app_user=Depends(get_current_app_user)
):
    # 1. Защита от дубликатов
    duplicate_stmt = select(Exercise).where(
        func.lower(Exercise.name) == payload.name.lower(),
        Exercise.app_user_id == current_app_user.id
    )
    duplicate_result = await db.execute(duplicate_stmt)
    if duplicate_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Упражнение с таким названием уже существует в вашем списке."
        )

    # === УМНАЯ КЛАССИФИКАЦИЯ ===

    # 1. Категория (Базовое / Изолирующее)
    has_secondary = bool(payload.secondary_muscle_groups and len(payload.secondary_muscle_groups) > 0)
    category_calc = "Базовое" if has_secondary else "Изолирующее"

    # 2. Сложность (на основе уровня пользователя)
    # Предполагаем, что у юзера есть поле experience_level (beginner, intermediate, advanced)
    user_level = getattr(current_app_user, "experience_level", "intermediate")
    if not user_level:
        user_level = "intermediate"

    difficulty_map = {
        "beginner": "Начинающий",
        "intermediate": "Средний",
        "advanced": "Сложный"
    }
    difficulty_calc = difficulty_map.get(user_level.lower(), "Средний")

    # 3. Fatigue Tier
    fatigue_tier_calc = calculate_fatigue_tier(
        category=category_calc,
        main_muscle=payload.main_muscle_group,
        secondary_muscles=payload.secondary_muscle_groups or [],
        equipment=payload.equipment_needed or []
    )

    # 4. Теги (Паттерны движения)
    tags = HeuristicsEngine.classify_exercise(payload.name, payload.main_muscle_group)

    # === СОЗДАНИЕ ОБЪЕКТА ===
    new_exercise = Exercise(
        name=payload.name,
        source="custom",
        app_user_id=current_app_user.id,
        category=category_calc,
        main_muscle_group=payload.main_muscle_group,
        secondary_muscle_groups=payload.secondary_muscle_groups or [],
        equipment_needed=payload.equipment_needed or [],
        description=payload.description,

        # Новые расчетные поля:
        difficulty=difficulty_calc,
        fatigue_tier=fatigue_tier_calc,

        # Распаковываем Enum в строки (используй .value или .name в зависимости от структуры твоего Enum)
        action=tags["action"].name if hasattr(tags["action"], "name") else str(tags["action"]),
        vector=tags["vector"].name if hasattr(tags["vector"], "name") else str(tags["vector"]),
        laterality=tags["laterality"].name if hasattr(tags["laterality"], "name") else str(tags["laterality"])
    )

    db.add(new_exercise)
    await db.commit()
    await db.refresh(new_exercise)

    return new_exercise