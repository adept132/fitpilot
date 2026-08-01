from typing import Optional, List

from sqlalchemy import func, select, case, desc, update
from fastapi import APIRouter, Depends, Query, HTTPException, status, Request
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
from api.services.models import Exercise, WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet, AppUser, WorkoutPlanExercise, UserExerciseNote
from api.schemas.exercises import (
    ExerciseListItemResponse,
    ExerciseDetailResponse,
    ExerciseHistoryItemResponse,
    ExerciseHistoryWorkoutDetailResponse,
    ExerciseHistoryWorkoutSetResponse,
    ExerciseLastPerformanceResponse, ExerciseSearchItem, MuscleGroupItem, LastWorkoutResponse,
    ExerciseAlternativeResponse, ReplaceExerciseRequest, CustomExerciseCreate,
    ExerciseNoteRequest, ExerciseNoteResponse,
    ExerciseClassifyRequest, ExerciseClassifyResponse,
)

router = APIRouter(tags=["exercises"])


def _thumb_url(request: Request, image_urls, image_approx) -> tuple[Optional[str], bool]:
    """Первое фото техники -> абсолютный URL миниатюры + флаг «примерная».
    Работает и для ORM-объекта, и для dict (ветка поиска через ExerciseMatcher)."""
    urls = image_urls or []
    approx = bool(image_approx)
    if not urls:
        return None, approx
    base = str(request.base_url).rstrip("/")
    return f"{base}/media/{str(urls[0]).lstrip('/')}", approx


@router.get("/exercises", response_model=list[ExerciseListItemResponse])
async def list_exercises(
        request: Request,
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
            image_url, image_approx = _thumb_url(
                request, item.get("image_urls"), item.get("image_approx")
            )
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
                    image_url=image_url,
                    image_approx=image_approx,
                )
            )
        else:
            image_url, image_approx = _thumb_url(
                request, item.image_urls, item.image_approx
            )
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
                    image_url=image_url,
                    image_approx=image_approx,
                )
            )

    return response_items

@router.get("/exercises/{exercise_id}", response_model=ExerciseDetailResponse)
async def get_exercise_detail(
    exercise_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    # Используем безопасный базовый запрос
    stmt = get_base_exercise_query(app_user.id).where(Exercise.id == exercise_id)
    result = await session.execute(stmt)
    exercise = result.scalar_one_or_none()

    if exercise is None:
        raise HTTPException(status_code=404, detail="Exercise not found")

    # Личная заметка текущего пользователя к этому упражнению
    note_row = await session.execute(
        select(UserExerciseNote.note).where(
            UserExerciseNote.app_user_id == app_user.id,
            UserExerciseNote.exercise_id == exercise_id,
        )
    )
    note = note_row.scalar_one_or_none()

    # Относительные пути из БД -> абсолютные URL на нашу статику (/media/...).
    # base_url уже включает схему и хост, поэтому host в БД не хардкодим.
    base = str(request.base_url).rstrip("/")
    image_urls = [
        f"{base}/media/{path.lstrip('/')}"
        for path in (exercise.image_urls or [])
    ]

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
        image_urls=image_urls,
        image_approx=bool(exercise.image_approx),
        note=note,
    )


@router.put("/exercises/{exercise_id}/note", response_model=ExerciseNoteResponse)
async def update_exercise_note(
    exercise_id: int,
    payload: ExerciseNoteRequest,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    # Проверяем, что упражнение доступно пользователю
    exists = await session.execute(
        get_base_exercise_query(app_user.id).where(Exercise.id == exercise_id)
    )
    if exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Exercise not found")

    note_text = payload.note.strip()

    existing = await session.execute(
        select(UserExerciseNote).where(
            UserExerciseNote.app_user_id == app_user.id,
            UserExerciseNote.exercise_id == exercise_id,
        )
    )
    row = existing.scalar_one_or_none()

    if row is None:
        row = UserExerciseNote(
            app_user_id=app_user.id,
            exercise_id=exercise_id,
            note=note_text,
        )
        session.add(row)
    else:
        row.note = note_text

    await session.commit()
    return ExerciseNoteResponse(note=note_text)

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
                parent_set_id=s.parent_set_id
            )
            for s in completed_sets
        ],
    )

@router.get("/search", response_model=list[ExerciseSearchItem])
async def search_exercises(
    request: Request,
    q: Optional[str] = None,
    muscle_group: Optional[str] = None,
    type: Optional[str] = None,
    equipment: Optional[str] = None,
    recent: bool = False,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(get_current_app_user),
):
    results = await ExerciseSearchService.search_exercises(
        session=session,
        user_id=user.id,
        q=q,
        muscle_group=muscle_group,
        type=type,
        equipment=equipment,
        recent=recent,
    )

    # Сервис отдаёт либо ORM-объекты (без q), либо dict (ветка ExerciseMatcher).
    # Приводим к единой схеме и подставляем абсолютный URL миниатюры.
    items: list[ExerciseSearchItem] = []
    for it in results:
        if isinstance(it, dict):
            image_url, image_approx = _thumb_url(
                request, it.get("image_urls"), it.get("image_approx")
            )
            items.append(ExerciseSearchItem(
                id=it.get("id"),
                name=it.get("name"),
                main_muscle_group=it.get("main_muscle_group") or "unknown",
                secondary_muscle_groups=it.get("secondary_muscle_groups") or [],
                category=it.get("category") or "base",
                equipment_needed=it.get("equipment_needed"),
                source=it.get("source") or "default",
                image_url=image_url,
                image_approx=image_approx,
            ))
        else:
            image_url, image_approx = _thumb_url(
                request, it.image_urls, it.image_approx
            )
            items.append(ExerciseSearchItem(
                id=it.id,
                name=it.name,
                main_muscle_group=it.main_muscle_group,
                secondary_muscle_groups=it.secondary_muscle_groups or [],
                category=it.category,
                equipment_needed=it.equipment_needed,
                source=it.source,
                image_url=image_url,
                image_approx=image_approx,
            ))
    return items


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

    # 4. Формируем ответ, склеивая объект Exercise и вычисленный score.
    #    Причины совпадения выводим из полей (мышца — жёсткий фильтр, поэтому
    #    в match_reasons не дублируется; её фронт берёт из main_muscle_group).
    #    unknown исключаем: два неклассифицированных упражнения не образуют
    #    осмысленного совпадения, хотя в score их равенство и даёт баллы.
    target_equipment = set(target_ex.equipment_needed or [])

    alternatives = []
    for ex_obj, score in rows:
        # Enum'ы наследуют str, поэтому сравнение со строкой "unknown" работает
        # и для enum-инстанса, и для сырой строки — не зависим от десериализации.
        reasons = []
        if ex_obj.action == target_ex.action and ex_obj.action != "unknown":
            reasons.append("pattern")
        if ex_obj.vector == target_ex.vector and ex_obj.vector != "unknown":
            reasons.append("direction")
        if target_equipment and target_equipment & set(ex_obj.equipment_needed or []):
            reasons.append("equipment")

        alt_data = ExerciseAlternativeResponse(
            id=ex_obj.id,
            name=ex_obj.name,
            main_muscle_group=ex_obj.main_muscle_group,
            equipment_needed=ex_obj.equipment_needed,
            match_score=score,
            match_reasons=reasons,
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

    # === Защита от дубликатов ===
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

    # Запоминаем исходное упражнение ДО подмены — понадобится для замены в плане.
    old_exercise_id = target_session_ex.exercise_id
    all_sets = sorted(target_session_ex.sets, key=lambda s: s.set_number)

    # ==========================================
    # ЛОГИКА ЗАМЕНЫ В СЕССИИ
    # ==========================================
    if not all_sets:
        # Нет подходов -> свап на месте. order_index и superset_group не трогаем.
        #
        # P0-06 C1, осознанно отложено: движок здесь НЕ пересчитывается.
        # target_session_ex.exercise_id меняется на другое упражнение, но
        # write-once в persist_prescription не даёт переписать уже
        # сохранённое prescription — если оно было посчитано для старого
        # упражнения (add_exercise_to_workout вызывает движок сразу при
        # добавлении), после свапа оно останется привязанным к строке, но
        # будет описывать УЖЕ НЕ ТО упражнение. Корректная починка требует
        # решения, как именно инвалидировать/пересчитать prescription при
        # замене (сбросить и пересчитать заново, что противоречит духу
        # write-once, или завести отдельное поле версии упражнения в
        # предписании) — отдельная задача, не C1/C2/C3 этого захода.
        target_session_ex.exercise_id = payload.new_exercise_id
        session_mode = "full_replace"
        session_status = "replaced"
    else:
        # Есть подходы -> старое упражнение остаётся с его подходами как реальная
        # проделанная работа, новое упражнение добавляем сразу следом с пустыми подходами.
        #
        # Освобождаем слот target.order_index + 1 сдвигом последующих упражнений на +1.
        # Прямой "order_index + 1" одним UPDATE нарушает уникальный индекс
        # (workout_session_id, order_index) — строка 2->3 пишется, пока 3 ещё занята.
        # Поэтому сдвигаем через временное большое смещение: каждый UPDATE оставляет
        # БД в бесконфликтном состоянии.
        target_order = target_session_ex.order_index
        OFFSET = 1_000_000

        # 1. Уводим последующие в высокий диапазон (коллизий с 1..N нет).
        await db.execute(
            update(WorkoutSessionExercise)
            .where(
                WorkoutSessionExercise.workout_session_id == session_id,
                WorkoutSessionExercise.order_index > target_order,
            )
            .values(order_index=WorkoutSessionExercise.order_index + OFFSET)
        )

        # 2. Вставляем новое упражнение в освободившийся слот сразу после старого.
        #
        # P0-06 C1, осознанно отложено: движок здесь НЕ вызывается, у
        # new_session_ex prescription остаётся пустым. Отличие от свободного
        # добавления (add_exercise_to_workout, workouts.py) в том, что это
        # "замена в середине тренировки" — нет загруженного профиля
        # (experience_level/settings) и нет резолва фазы мезоцикла в этом
        # месте кода, оба надо тащить сюда так же, как в C1 для start_workout.
        # Решение: отложить, а не тихо оставить как есть — пользователь,
        # которому заменили упражнение НЕ в конце тренировки (веток
        # "keep_and_add"), увидит его без цели до следующего раза, когда его
        # добавят явно через add_exercise_to_workout или до финиша сессии
        # (finish_workout считает next_prescription, но не prescription
        # текущей сессии). Зафиксировано как известный разрыв для отдельной
        # задачи, а не тихий пропуск.
        new_session_ex = WorkoutSessionExercise(
            workout_session_id=session_id,
            exercise_id=payload.new_exercise_id,
            order_index=target_order + 1,
            superset_group=target_session_ex.superset_group,  # Копируем ID суперсета!
            target_sets=target_session_ex.target_sets,
            notes=None,
        )
        db.add(new_session_ex)
        await db.flush()

        # 3. Возвращаем последующие обратно, теперь они встают за новым (target+2, ...).
        await db.execute(
            update(WorkoutSessionExercise)
            .where(
                WorkoutSessionExercise.workout_session_id == session_id,
                WorkoutSessionExercise.order_index > OFFSET,
            )
            .values(order_index=WorkoutSessionExercise.order_index - (OFFSET - 1))
        )

        session_mode = "keep_and_add"
        session_status = "added"

    # ==========================================
    # ЗАМЕНА В ПЛАНЕ (опционально)
    # ==========================================
    plan_updated = False
    if payload.update_plan:
        workout = await db.get(WorkoutSession, session_id)
        if workout and workout.plan_id:
            plan_ex_result = await db.execute(
                select(WorkoutPlanExercise)
                .where(WorkoutPlanExercise.plan_id == workout.plan_id)
                .order_by(WorkoutPlanExercise.order_index)
            )
            plan_exercises = list(plan_ex_result.scalars().all())

            # Не дублируем: пропускаем, если новое упражнение уже есть в плане.
            already_in_plan = any(
                pe.exercise_id == payload.new_exercise_id for pe in plan_exercises
            )
            old_plan_ex = next(
                (pe for pe in plan_exercises if pe.exercise_id == old_exercise_id),
                None,
            )
            if old_plan_ex and not already_in_plan:
                old_plan_ex.exercise_id = payload.new_exercise_id
                plan_updated = True

    await db.commit()
    return {"status": session_status, "mode": session_mode, "plan_updated": plan_updated}


@router.post("/exercises/classify", response_model=ExerciseClassifyResponse)
async def classify_exercise(
    payload: ExerciseClassifyRequest,
    app_user: AppUser = Depends(get_current_app_user),
):
    """Авто-заполнение: по названию предлагает мышцы/вторичные/оборудование.
    kNN по мультиязычным эмбеддингам с фолбэком на разбор названия."""
    from api.services.exercise_classifier import classify

    return ExerciseClassifyResponse(**classify(payload.name))


@router.post("/exercises", response_model=ExerciseSearchItem, status_code=status.HTTP_201_CREATED)
async def create_custom_exercise(
        payload: CustomExerciseCreate,
        db: AsyncSession = Depends(get_db),
        current_app_user=Depends(get_current_app_user)
):
    # 0. Идемпотентность offline-повтора: если упражнение с этим client_uuid уже
    # создано — возвращаем его, а не 409/дубль.
    if getattr(payload, "client_uuid", None):
        existing = (await db.execute(
            select(Exercise).where(
                Exercise.app_user_id == current_app_user.id,
                Exercise.client_uuid == payload.client_uuid,
            )
        )).scalar_one_or_none()
        if existing:
            return existing

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
        client_uuid=getattr(payload, "client_uuid", None),
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