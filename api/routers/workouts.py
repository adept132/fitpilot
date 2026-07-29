from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_db
from api.schemas.supersets import CreateSupersetRequest, ReorderWorkoutStructureRequest
from api.schemas.workouts import WorkoutSessionDetailResponse, AddWorkoutExerciseRequest, AddWorkoutSetResponse, \
    AddWorkoutSetRequest, UpdateWorkoutSetRequest, RepeatWorkoutSetRequest, AutoprogressionResponse
from api.services.anomaly_guard import check_set, resolve_is_anomalous
from api.services.anomaly_stats import load_exercise_stats
from api.services.app_user_service import get_current_app_user
from api.services.autoprogression import compute_autoprogression
from api.services.calculate_exercise_recommendation import calculate_exercise_recommendations
from api.services.exercise_utils import get_base_exercise_query
from api.services.progression import repository as progression_repo
from api.services.progression.engine import plan_exercise
from api.services.progression.resolve import override_for
from api.services.workout_superset_service import WorkoutSupersetService
from api.services.models import WorkoutSession, WorkoutSessionExercise, Exercise, WorkoutSessionSet, AppUser, \
    AppUserProfile, AppUserMesocycle, MesocyclePhase

router = APIRouter(tags=["workouts"])


def _workout_session_options():
    """Загружает упражнения сессии, а также связанные с ними данные (каталог и подходы) за один проход."""
    return selectinload(WorkoutSession.exercises).options(
        selectinload(WorkoutSessionExercise.exercise),
        selectinload(WorkoutSessionExercise.sets)
    )

def _renumber_sets(session_exercise: WorkoutSessionExercise) -> None:
    ordered_sets = sorted(session_exercise.sets, key=lambda s: s.set_number)
    for index, workout_set in enumerate(ordered_sets, start=1):
        workout_set.set_number = index


@router.get(
    "/workouts/active",
    response_model=WorkoutSessionDetailResponse,
)
async def get_active_workout(
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    workout = await get_active_workout_for_user(db, current_app_user.id)

    if workout is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active workout",
        )

    return workout

@router.get(
    "/workouts/{workout_id}",
    response_model=WorkoutSessionDetailResponse,
)
async def get_workout_detail(
    workout_id: int,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    print("GET DETAIL INPUT:", {
        "workout_id": workout_id,
        "current_app_user_id": current_app_user.id,
    })

    raw_stmt = select(WorkoutSession).where(WorkoutSession.id == workout_id)
    raw_result = await db.execute(raw_stmt)
    raw_workout = raw_result.scalar_one_or_none()

    print("RAW WORKOUT:", raw_workout)
    if raw_workout is not None:
        print("RAW WORKOUT DATA:", {
            "id": raw_workout.id,
            "app_user_id": raw_workout.app_user_id,
            "source": raw_workout.source,
            "status": raw_workout.status,
            "split_day_id": raw_workout.split_day_id,
        })

    stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.id == workout_id,
            WorkoutSession.app_user_id == current_app_user.id,
        )
        # === ИСПОЛЬЗУЙ ЭТОТ БЛОК ВМЕСТО СТАРЫХ ФУНКЦИЙ ===
        .options(
            _workout_session_options()
        )
    )

    result = await db.execute(stmt)
    workout = result.scalar_one_or_none()

    print("FILTERED WORKOUT:", workout)

    if workout is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout not found",
        )

    return workout


@router.delete(
    "/workouts/{workout_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_workout(
    workout_id: int,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    """Отмена тренировки: каскадно удаляет упражнения и подходы сессии."""
    stmt = select(WorkoutSession).where(WorkoutSession.id == workout_id)
    result = await db.execute(stmt)
    workout = result.scalar_one_or_none()

    if workout is None or workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout not found",
        )

    await db.delete(workout)
    await db.commit()


@router.post(
    "/workouts/{workout_id}/exercises",
    response_model=WorkoutSessionDetailResponse,
)
async def add_exercise_to_workout(
        workout_id: int,
        payload: AddWorkoutExerciseRequest,
        db: AsyncSession = Depends(get_db),
        current_app_user=Depends(get_current_app_user),
):
    workout_stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.id == workout_id,
            WorkoutSession.app_user_id == current_app_user.id,
        )
        .options(
            selectinload(WorkoutSession.exercises),
        )
    )

    workout_result = await db.execute(workout_stmt)
    workout = workout_result.scalar_one_or_none()

    if workout is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    exercise_stmt = get_base_exercise_query(current_app_user.id).where(Exercise.id == payload.exercise_id)
    exercise_result = await db.execute(exercise_stmt)
    exercise = exercise_result.scalar_one_or_none()

    if exercise is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Exercise not found",
        )

    existing_session_exercise = next(
        (item for item in workout.exercises if item.exercise_id == payload.exercise_id),
        None,
    )

    if existing_session_exercise is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Exercise already added to this workout",
        )

    # --- ИСПРАВЛЕННАЯ ЛОГИКА РЕОРДЕРА ---

    # 1. Надежное вычисление индекса для автоматического добавления в конец.
    if workout.exercises:
        max_order = max((ex.order_index for ex in workout.exercises if ex.order_index is not None), default=-1)
        next_order_index = max_order + 1
    else:
        next_order_index = 0

    fatigue_tier = getattr(exercise, "fatigue_tier", 2)
    recommendations = await calculate_exercise_recommendations(
        db, current_app_user.id, single_exercise_id=exercise.id, single_fatigue_tier=fatigue_tier
    )
    rec_data = recommendations[0] if recommendations else {}

    session_exercise = WorkoutSessionExercise(
        workout_session_id=workout.id,
        exercise_id=payload.exercise_id,
        order_index=next_order_index,
        superset_group=payload.superset_group,
        notes=payload.notes,
        # ДОБАВЛЯЕМ СНИМКИ
        # (фикс несовпадения ключей: calculate_exercise_recommendations возвращает
        # "recommended_rir"/"recommended_rep_min"/"recommended_rep_max", а не "rir"/"rmin"/"rmax" —
        # из-за этого поля всегда были null, и автопрогрессия ниже не имела rep_candidates)
        recommended_rir=rec_data.get("recommended_rir"),
        recommended_rep_min=rec_data.get("recommended_rep_min"),
        recommended_rep_max=rec_data.get("recommended_rep_max"),
    )

    db.add(session_exercise)
    await db.flush()

    # Рекомендованный вес заполняем при добавлении упражнения, чтобы накапливать
    # объективную метку «предписано против сделано» (см. spec §8.3).
    profile_result = await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_app_user.id)
    )
    profile = profile_result.scalars().first()
    experience_level = profile.experience_level if profile else None
    settings = profile.settings if profile else None

    # exercise уже загружен выше — присваиваем явно, чтобы compute_autoprogression
    # не пытался сделать ленивую подгрузку session_exercise.exercise (запрещено в async SQLAlchemy).
    session_exercise.exercise = exercise

    # Предписание считаем сразу: пользователь должен увидеть цель до первого
    # подхода, а evaluate() потом сравнит факт именно с ней.
    ctx = await progression_repo.build_context(
        db, session_exercise, current_app_user.id, experience_level, settings
    )
    prescription = plan_exercise(
        ctx, override=override_for(settings, session_exercise.exercise_id)
    )
    progression_repo.persist_prescription(session_exercise, prescription)

    await db.commit()

    detail_stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.id == workout_id,
            WorkoutSession.app_user_id == current_app_user.id,
        )
        .options(
            _workout_session_options()
        )
        # populate_existing: `workout` (тот же объект, что вернёт этот запрос —
        # SQLAlchemy identity map) уже был загружен ВЫШЕ с selectinload(exercises),
        # который в момент загрузки был пуст (упражнение ещё не добавлено). Раз
        # коллекция уже помечена загруженной, а expire_on_commit=False (см.
        # app/database.py), commit() её не инвалидирует — без populate_existing
        # повторный SELECT ниже молча вернул бы тот же устаревший пустой список
        # вместо только что добавленного упражнения.
        .execution_options(populate_existing=True)
    )

    detail_result = await db.execute(detail_stmt)
    updated_workout = detail_result.scalar_one()

    return updated_workout


@router.post(
    "/workout-session-exercises/{session_exercise_id}/sets",
    response_model=AddWorkoutSetResponse,
)
async def add_set_to_session_exercise(
    session_exercise_id: int,
    payload: AddWorkoutSetRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    print("ADD SET PAYLOAD:", payload.model_dump())

    stmt = (
        select(WorkoutSessionExercise)
        .where(WorkoutSessionExercise.id == session_exercise_id)
        .options(
            selectinload(WorkoutSessionExercise.workout_session),
            selectinload(WorkoutSessionExercise.sets),
        )
    )

    result = await db.execute(stmt)
    session_exercise = result.scalar_one_or_none()

    if session_exercise is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    workout = session_exercise.workout_session

    if workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    if payload.parent_set_id is not None:
        parent_exists = any(s.id == payload.parent_set_id for s in session_exercise.sets)
        if not parent_exists:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent set does not belong to this exercise",
            )

    next_set_number = len(session_exercise.sets) + 1

    stats = await load_exercise_stats(
        db, current_app_user.id, session_exercise.exercise_id
    )
    verdict = check_set(
        float(payload.weight) if payload.weight is not None else None,
        payload.reps,
        payload.set_type or "normal",
        stats,
    )
    is_anomalous = resolve_is_anomalous(verdict, payload.anomaly_confirmed)

    new_set = WorkoutSessionSet(
        workout_session_exercise_id=session_exercise.id,
        set_number=next_set_number,
        set_type=payload.set_type,
        weight=payload.weight,
        reps=payload.reps,
        effort_level=payload.effort_level,
        notes=payload.notes,
        parent_set_id=payload.parent_set_id,
        superset_round=payload.superset_round,
        is_completed=True,
        is_anomalous=is_anomalous,
    )

    print(
        "NEW SET VALUES:",
        {
            "set_number": next_set_number,
            "weight": payload.weight,
            "reps": payload.reps,
            "effort_level": payload.effort_level,
        },
    )

    db.add(new_set)
    await db.commit()
    await db.refresh(new_set)

    return new_set

@router.get(
    "/workout-session-exercises/{session_exercise_id}/autoprogression",
    response_model=AutoprogressionResponse,
)
async def get_exercise_autoprogression(
    session_exercise_id: int,
    target_reps: int | None = Query(default=None, gt=0),
    target_effort: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    """Рекомендация автопрогрессии для упражнения в текущей сессии.

    target_reps/target_effort передаются только в свободной тренировке (пользователь
    выбирает их вручную). В плановой берём целевые параметры из сессии.
    """
    stmt = (
        select(WorkoutSessionExercise)
        .where(WorkoutSessionExercise.id == session_exercise_id)
        .options(
            selectinload(WorkoutSessionExercise.workout_session),
            selectinload(WorkoutSessionExercise.exercise),
        )
    )
    result = await db.execute(stmt)
    session_exercise = result.scalar_one_or_none()

    if session_exercise is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    if session_exercise.workout_session.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    profile_result = await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_app_user.id)
    )
    profile = profile_result.scalars().first()
    experience_level = profile.experience_level if profile else None
    settings = profile.settings if profile else None

    # Фаза мезоцикла нужна схеме percent_1rm и правилу deload_phase, но обе
    # сравнивают со строковым effort_tier ("prefailure"/"failure"/"deload"/...),
    # а не с номером фазы. WorkoutSession.mesocycle_phase — это НОМЕР (int), не
    # название (см. развёрнутый комментарий в
    # api/services/progression/repository.py::_load_deload_map) — просто
    # str(...) от него дал бы "1"/"2"/... и обе проверки молча никогда бы не
    # сработали. Тот же join по (app_user_mesocycle_id, phase_number), что и в
    # _load_deload_map, но для одной сессии — репозиторий трогать нельзя
    # (не в списке файлов задачи), поэтому запрос локальный.
    phase_effort_tier = "medium"
    workout_session = session_exercise.workout_session
    if (
        workout_session.app_user_mesocycle_id is not None
        and workout_session.mesocycle_phase is not None
    ):
        tier_stmt = (
            select(MesocyclePhase.effort_tier)
            .join(
                AppUserMesocycle,
                AppUserMesocycle.mesocycle_id == MesocyclePhase.mesocycle_id,
            )
            .where(
                AppUserMesocycle.id == workout_session.app_user_mesocycle_id,
                MesocyclePhase.phase_number == workout_session.mesocycle_phase,
            )
        )
        tier = (await db.execute(tier_stmt)).scalar_one_or_none()
        if tier is not None:
            phase_effort_tier = tier

    data = await compute_autoprogression(
        db,
        session_exercise,
        current_app_user.id,
        experience_level,
        settings,
        target_reps=target_reps,
        target_effort=target_effort,
        phase_effort_tier=phase_effort_tier,
    )

    return AutoprogressionResponse(**data)


async def get_active_workout_for_user(
    db: AsyncSession,
    app_user_id: int,
) -> WorkoutSession | None:
    stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "active",
        )
        .options(
            _workout_session_options()
        )
        .order_by(WorkoutSession.started_at.desc())
    )

    result = await db.execute(stmt)
    return result.scalars().first()

@router.patch(
    "/workout-session-sets/{set_id}",
    response_model=AddWorkoutSetResponse,
)
async def update_workout_session_set(
    set_id: int,
    payload: UpdateWorkoutSetRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSessionSet)
        .where(WorkoutSessionSet.id == set_id)
        .options(
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.workout_session),
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.sets),
        )
    )

    result = await db.execute(stmt)
    workout_set = result.scalar_one_or_none()

    if workout_set is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    session_exercise = workout_set.workout_session_exercise
    workout = session_exercise.workout_session

    if workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    update_data = payload.model_dump(exclude_unset=True)

    # anomaly_confirmed — флаг запроса, а не колонка модели. Если оставить его
    # в update_data, цикл setattr ниже повесит посторонний атрибут на ORM-объект.
    anomaly_confirmed = bool(update_data.pop("anomaly_confirmed", False))

    if "parent_set_id" in update_data and update_data["parent_set_id"] is not None:
        parent_exists = any(s.id == update_data["parent_set_id"] for s in session_exercise.sets)
        if not parent_exists:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent set does not belong to this exercise",
            )

    for field_name, value in update_data.items():
        setattr(workout_set, field_name, value)

    stats = await load_exercise_stats(
        db, current_app_user.id, session_exercise.exercise_id
    )
    verdict = check_set(
        float(workout_set.weight) if workout_set.weight is not None else None,
        workout_set.reps,
        workout_set.set_type or "normal",
        stats,
    )
    workout_set.is_anomalous = resolve_is_anomalous(verdict, anomaly_confirmed)

    await db.commit()
    await db.refresh(workout_set)

    return workout_set

@router.post(
    "/workout-session-sets/{set_id}/repeat",
    response_model=AddWorkoutSetResponse,
)
async def repeat_workout_session_set(
    set_id: int,
    payload: RepeatWorkoutSetRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSessionSet)
        .where(WorkoutSessionSet.id == set_id)
        .options(
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.workout_session),
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.sets),
        )
    )

    result = await db.execute(stmt)
    source_set = result.scalar_one_or_none()

    if source_set is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    source_session_exercise = source_set.workout_session_exercise
    workout = source_session_exercise.workout_session

    if workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    target_session_exercise = source_session_exercise

    if payload is not None and payload.target_session_exercise_id is not None:
        target_stmt = (
            select(WorkoutSessionExercise)
            .where(WorkoutSessionExercise.id == payload.target_session_exercise_id)
            .options(
                selectinload(WorkoutSessionExercise.workout_session),
                selectinload(WorkoutSessionExercise.sets),
            )
        )
        target_result = await db.execute(target_stmt)
        target_session_exercise = target_result.scalar_one_or_none()

        if target_session_exercise is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target workout session exercise not found",
            )

        if target_session_exercise.workout_session.app_user_id != current_app_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target workout session exercise not found",
            )

        if target_session_exercise.workout_session.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workout is not active",
            )

    next_set_number = len(target_session_exercise.sets) + 1

    repeated_set = WorkoutSessionSet(
        workout_session_exercise_id=target_session_exercise.id,
        set_number=next_set_number,
        set_type=source_set.set_type,
        weight=source_set.weight,
        reps=source_set.reps,
        effort_level=source_set.effort_level,
        notes=source_set.notes,
        parent_set_id=None,
        superset_round=source_set.superset_round,
        is_completed=True,
        # Повтор наследует пометку исходного подхода: пересчитывать вердикт
        # заново нельзя — это стёрло бы подтверждение, которое пользователь
        # уже дал по оригиналу.
        is_anomalous=source_set.is_anomalous,
    )

    db.add(repeated_set)
    await db.commit()
    await db.refresh(repeated_set)

    return repeated_set

@router.delete(
    "/workout-session-exercises/{session_exercise_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_workout_session_exercise(
    session_exercise_id: int,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSessionExercise)
        .where(WorkoutSessionExercise.id == session_exercise_id)
        .options(
            selectinload(WorkoutSessionExercise.workout_session)
            .selectinload(WorkoutSession.exercises)
        )
    )

    result = await db.execute(stmt)
    session_exercise = result.scalar_one_or_none()

    if session_exercise is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    workout = session_exercise.workout_session

    if workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    deleted_exercise_id = session_exercise.id

    await db.delete(session_exercise)
    await db.flush()

    remaining_exercises = sorted(
        [e for e in workout.exercises if e.id != deleted_exercise_id],
        key=lambda e: e.order_index,
    )

    for index, exercise in enumerate(remaining_exercises):
        exercise.order_index = index

    await db.commit()

@router.delete(
    "/workout-session-sets/{set_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_workout_session_set(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSessionSet)
        .where(WorkoutSessionSet.id == set_id)
        .options(
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.workout_session),
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.sets),
        )
    )

    result = await db.execute(stmt)
    workout_set = result.scalar_one_or_none()

    if workout_set is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    session_exercise = workout_set.workout_session_exercise
    workout = session_exercise.workout_session

    if workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    deleted_set_id = workout_set.id

    await db.delete(workout_set)
    await db.flush()

    session_exercise.sets = [
        s for s in session_exercise.sets if s.id != deleted_set_id
    ]
    _renumber_sets(session_exercise)

    await db.commit()

@router.post("/workouts/{workout_id}/supersets")
async def create_superset(
    workout_id: int,
    payload: CreateSupersetRequest,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    superset_group = await WorkoutSupersetService.create_superset(
        session=session,
        workout_id=workout_id,
        app_user_id=app_user.id,
        source_session_exercise_id=payload.source_session_exercise_id,
        target_session_exercise_ids=payload.target_session_exercise_ids,
    )

    return {
        "superset_group": superset_group,
    }

@router.patch("/workouts/{workout_id}/structure")
async def reorder_workout_structure(
    workout_id: int,
    payload: ReorderWorkoutStructureRequest,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    await WorkoutSupersetService.reorder_workout_structure(
        session=session,
        workout_id=workout_id,
        app_user_id=app_user.id,
        items=payload.items,
    )
    return {"ok": True}