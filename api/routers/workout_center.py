from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload

from api.deps import get_db
from api.schemas.mesocycle import UpdateSelectedMesocyclePayload, UpdateMesocyclePhasePayload, \
    UpdateMesocycleContextPayload
from api.schemas.microcycle import UpdateMicrocycleContextPayload
from api.schemas.plan import VolumeTargetsResponse, MuscleTarget, UpdatePlanContextPayload
from api.schemas.workouts import FinishWorkoutResponse, WorkoutFinishedExerciseSummary
from api.services.app_user_service import get_current_app_user
from api.services.calculate_exercise_recommendation import calculate_exercise_recommendations

# ОБНОВЛЕННЫЕ ИМПОРТЫ МОДЕЛЕЙ БАЗЫ ДАННЫХ
from api.services.models import (
    AppUser,
    UserSplit,
    SplitBlueprint,
    SplitDaySlot,
    DayBlueprint,
    DayMuscleTarget,
    WorkoutSession,
    WorkoutSessionExercise, AppUserMesocycle, Mesocycle, AppUserProfile, WorkoutPlan, AppUserMicrocycle,
    WorkoutSessionSet, UserCalendarDay,
)
from api.schemas.workout_center import (
    WorkoutCenterContextRead,
    WorkoutCenterSplitRead,
    WorkoutCenterSplitDayRead,
    WorkoutCenterActiveWorkoutRead,
    UpdateSelectedSplitPayload,
    UpdateSelectedSplitDayPayload,
    StartWorkoutPayload,
    StartWorkoutResponse, WorkoutCenterMesocycleRead,
)
from api.services.volume_service import VolumeService

router = APIRouter(prefix="", tags=["workout-center"])


def build_split_day_read(slot: SplitDaySlot) -> WorkoutCenterSplitDayRead:
    # Вытаскиваем мышцы напрямую из нового массива кубика
    primary = [m.muscle_group_id for m in slot.day.muscle_targets]

    return WorkoutCenterSplitDayRead(
        id=slot.id,  # Теперь в качестве ID дня выступает уникальный ID слота (UUID)
        name=slot.day.name,
        day_number=slot.day_order,
        primary_muscles=primary,
        secondary_muscles=[],  # В новой архитектуре у нас пока нет жесткого разделения на вторичные
    )


async def get_or_create_user_split(
        session: AsyncSession,
        app_user: AppUser,
) -> UserSplit | None:
    stmt = (
        select(UserSplit)
        .where(UserSplit.app_user_id == app_user.id, UserSplit.is_active == True)
        .options(
            # Подгружаем новую иерархию чертежей
            selectinload(UserSplit.blueprint)
            .selectinload(SplitBlueprint.slots)
            .selectinload(SplitDaySlot.day)
            .selectinload(DayBlueprint.muscle_targets)
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_active_workout(
        session: AsyncSession,
        app_user_id: int,
) -> WorkoutSession | None:
    stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "active",
        )
        .order_by(WorkoutSession.started_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def build_context(
        session: AsyncSession,
        app_user: AppUser,
) -> WorkoutCenterContextRead:
    # --- 1. ЗАГРУЗКА СПЛИТОВ ---
    splits_stmt = (
        select(SplitBlueprint)
        .where(
            (SplitBlueprint.is_system == True) |
            (SplitBlueprint.author_id == app_user.id)
        )
        .order_by(SplitBlueprint.is_system.desc(), SplitBlueprint.name.asc())
    )
    splits_result = await session.execute(splits_stmt)
    available_splits = splits_result.scalars().all()

    user_split = await get_or_create_user_split(session, app_user)

    selected_split = None
    available_split_days = []
    selected_split_day = None

    if user_split and user_split.blueprint:
        selected_split = WorkoutCenterSplitRead(
            id=user_split.blueprint.id,
            name=user_split.blueprint.name,
        )

        sorted_slots = sorted(user_split.blueprint.slots, key=lambda s: s.day_order)
        available_split_days = [build_split_day_read(slot) for slot in sorted_slots]

        selected_slot = next(
            (slot for slot in sorted_slots if slot.day_order == user_split.current_day),
            None,
        )

        if selected_slot:
            selected_split_day = build_split_day_read(selected_slot)

    # --- 2. ЗАГРУЗКА МЕЗОЦИКЛОВ (ПЕРИОДИЗАЦИИ) ---

    # А. Получаем все доступные стратегии (шаблоны)
    mesos_stmt = select(Mesocycle).order_by(Mesocycle.id.asc())
    mesos_result = await session.execute(mesos_stmt)
    available_mesocycles_db = mesos_result.scalars().all()

    # Форматируем их в список словарей (или используем Pydantic схему, если она у тебя есть)
    available_mesocycles = [
        WorkoutCenterMesocycleRead(
            id=m.id,
            name=m.name,
            phases_in_cycle=m.phases_in_cycle  # <--- ДОБАВИЛИ НЕДОСТАЮЩЕЕ ПОЛЕ
        )
        for m in available_mesocycles_db
    ]

    # Б. Ищем активный мезоцикл текущего пользователя
    active_meso_stmt = (
        select(AppUserMesocycle)
        .options(
            joinedload(AppUserMesocycle.mesocycle)
            .selectinload(Mesocycle.phases)  # Подгружаем список фаз
        )
        .where(
            AppUserMesocycle.app_user_id == app_user.id,
            AppUserMesocycle.is_active == True
        )
        .limit(1)
    )
    active_meso_result = await session.execute(active_meso_stmt)
    active_meso = active_meso_result.scalar_one_or_none()

    selected_periodization = None
    selected_periodization_week = None
    phase_label = None

    if active_meso and active_meso.mesocycle:
        selected_periodization = WorkoutCenterMesocycleRead(
            id=active_meso.mesocycle.id,
            name=active_meso.mesocycle.name,
            phases_in_cycle=active_meso.mesocycle.phases_in_cycle,
            # ДОБАВЛЯЕМ ВОЗВРАТ СОХРАНЕННОЙ ДЛИНЫ
            microcycle_length=active_meso.microcycle_length
        )

        # Забираем текущую неделю из БД
        current_phase = active_meso.current_phase
        selected_periodization_week = current_phase

        # Находим имя фазы (защита: если недель больше, чем фаз, берем последнюю)
        # Находим имя фазы
        phases = sorted(active_meso.mesocycle.phases, key=lambda p: p.phase_number)
        if phases:
            target_phase = next((p for p in phases if p.phase_number == current_phase), phases[-1])
            phase_label = target_phase.name

        # --- 3. ЗАГРУЗКА ПЛАНОВ ---
    plans_stmt = select(WorkoutPlan).where(WorkoutPlan.app_user_id == app_user.id)
    plans_result = await session.execute(plans_stmt)
    available_plans_db = plans_result.scalars().all()

    # Формируем список для фронтенда (используем dict, чтобы Pydantic сам их распарсил)
    available_plans = [{"id": p.id, "name": p.name} for p in available_plans_db]
    selected_plan = None

    # План привязан к текущему дню сплита
    if user_split and user_split.selected_plans:
        current_day_str = str(user_split.current_day)
        plan_id = user_split.selected_plans.get(current_day_str)

        if plan_id:
            # Ищем план среди доступных
            sp = next((p for p in available_plans_db if str(p.id) == str(plan_id)), None)
            if sp:
                selected_plan = {"id": sp.id, "name": sp.name}

    # --- 4. АКТИВНАЯ ТРЕНИРОВКА ---
    active_workout = await get_active_workout(session, app_user.id)

    micro_stmt = select(AppUserMicrocycle).where(AppUserMicrocycle.app_user_id == app_user.id)
    micro_result = await session.execute(micro_stmt)
    available_micros_db = micro_result.scalars().all()

    # Формируем список для фронтенда
    available_microcycles = [{"id": m.id, "name": m.name} for m in available_micros_db]
    selected_microcycle = None

    # Ищем активный микроцикл среди загруженных
    active_micro = next((m for m in available_micros_db if m.is_active), None)
    if active_micro:
        selected_microcycle = {"id": active_micro.id, "name": active_micro.name}

    # --- АКТИВНАЯ ТРЕНИРОВКА ---
    active_workout = await get_active_workout(session, app_user.id)

    # ВОЗВРАЩАЕМ ИТОГОВЫЙ КОНТЕКСТ
    return WorkoutCenterContextRead(
        selected_split=selected_split,
        available_splits=[
            WorkoutCenterSplitRead(id=split.id, name=split.name)
            for split in available_splits
        ],
        selected_split_day=selected_split_day,
        available_split_days=available_split_days,

        selected_plan=selected_plan,
        available_plans=available_plans,

        selected_periodization=selected_periodization,
        selected_periodization_week=selected_periodization_week,
        selected_periodization_phase_name=phase_label,
        available_mesocycles=available_mesocycles,

        # ТЕПЕРЬ ПЕРЕДАЕМ ДАННЫЕ МИКРОЦИКЛОВ НА ФРОНТЕНД
        selected_microcycle=selected_microcycle,
        available_microcycles=available_microcycles,

        active_workout=(
            WorkoutCenterActiveWorkoutRead(
                id=active_workout.id,
                started_at=active_workout.started_at,
                source=active_workout.source,
            )
            if active_workout
            else None
        ),
    )


@router.get("/workout-center/context", response_model=WorkoutCenterContextRead)
async def get_workout_center_context(
        app_user: AppUser = Depends(get_current_app_user),
        session: AsyncSession = Depends(get_db),
):
    return await build_context(session, app_user)


@router.patch("/workout-center/split", response_model=WorkoutCenterContextRead)
async def update_workout_center_split(
        payload: UpdateSelectedSplitPayload,
        app_user: AppUser = Depends(get_current_app_user),
        session: AsyncSession = Depends(get_db),
):
    split_stmt = select(SplitBlueprint).where(SplitBlueprint.id == payload.split_id)
    split_result = await session.execute(split_stmt)
    blueprint = split_result.scalar_one_or_none()

    if not blueprint:
        raise HTTPException(status_code=404, detail="Split blueprint not found")

    stmt = select(UserSplit).where(
        UserSplit.app_user_id == app_user.id,
        UserSplit.is_active == True,
    )
    result = await session.execute(stmt)
    user_split = result.scalar_one_or_none()

    if user_split:
        user_split.blueprint_id = blueprint.id
        user_split.current_day = 1
        user_split.selected_plans = {}
    else:
        user_split = UserSplit(
            app_user_id=app_user.id,
            blueprint_id=blueprint.id,
            selected_plans={},
            is_active=True,
            current_day=1,
        )
        session.add(user_split)

    await session.commit()
    return await build_context(session, app_user)


@router.patch("/workout-center/split-day", response_model=WorkoutCenterContextRead)
async def update_workout_center_split_day(
        payload: UpdateSelectedSplitDayPayload,
        app_user: AppUser = Depends(get_current_app_user),
        session: AsyncSession = Depends(get_db),
):
    stmt = select(UserSplit).where(
        UserSplit.app_user_id == app_user.id,
        UserSplit.is_active == True,
    )
    result = await session.execute(stmt)
    user_split = result.scalar_one_or_none()

    if not user_split:
        raise HTTPException(
            status_code=400,
            detail="Active split is not selected",
        )

    slot_stmt = select(SplitDaySlot).where(
        SplitDaySlot.id == payload.split_day_id,
        SplitDaySlot.blueprint_id == user_split.blueprint_id,
    )
    slot_result = await session.execute(slot_stmt)
    slot = slot_result.scalar_one_or_none()

    if not slot:
        raise HTTPException(status_code=404, detail="Split day slot not found")

    user_split.current_day = slot.day_order
    await session.commit()

    return await build_context(session, app_user)


@router.post("/workouts/start", response_model=StartWorkoutResponse)
async def start_workout(
        payload: StartWorkoutPayload,
        app_user: AppUser = Depends(get_current_app_user),
        session: AsyncSession = Depends(get_db),
):
    if payload.source not in {"free", "by_parameters"}:
        raise HTTPException(status_code=400, detail="Invalid source")

    existing = await get_active_workout(session, app_user.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Active workout already exists",
        )

    split_day_tag = None

    if payload.source == "by_parameters":
        if not payload.split_id or not payload.split_day_id:
            raise HTTPException(status_code=400, detail="split_id and split_day_id are required")

        split_stmt = select(SplitBlueprint).where(SplitBlueprint.id == payload.split_id)
        split_result = await session.execute(split_stmt)
        if not split_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Split blueprint not found")

        slot_stmt = (
            select(SplitDaySlot)
            .options(selectinload(SplitDaySlot.day))
            .where(
                SplitDaySlot.id == payload.split_day_id,
                SplitDaySlot.blueprint_id == payload.split_id,
            )
        )
        slot_result = await session.execute(slot_stmt)
        slot = slot_result.scalar_one_or_none()
        if not slot:
            raise HTTPException(status_code=404, detail="Split day slot not found")

        split_day_tag = slot.day.template_type.value if hasattr(slot.day.template_type, 'value') else str(
            slot.day.template_type)

    # === ИНТЕЛЛЕКТУАЛЬНЫЙ ПОИСК КОНТЕКСТА ===
    meso_id = None
    current_phase = None
    micro_id = None

    # 1. Если это тренировка из Календаря, берем ИДЕАЛЬНЫЕ данные от движка
    if payload.calendar_day_id:
        cal_stmt = select(UserCalendarDay).where(UserCalendarDay.id == payload.calendar_day_id)
        cal_day = (await session.execute(cal_stmt)).scalar_one_or_none()
        if cal_day:
            meso_id = cal_day.user_mesocycle_id
            current_phase = cal_day.mesocycle_phase_number
            micro_id = cal_day.user_microcycle_id

            # Можно сразу перевести день календаря в статус "completed" или "in_progress"
            # cal_day.status = "in_progress"

    # 2. Если это полностью свободная тренировка, берем глобальные активные циклы
    if not meso_id and not micro_id:
        active_meso = (await session.execute(
            select(AppUserMesocycle).where(AppUserMesocycle.app_user_id == app_user.id,
                                           AppUserMesocycle.is_active == True)
        )).scalar_one_or_none()
        meso_id = active_meso.id if active_meso else None
        current_phase = active_meso.current_phase if active_meso else None

        active_micro = (await session.execute(
            select(AppUserMicrocycle).where(AppUserMicrocycle.app_user_id == app_user.id,
                                            AppUserMicrocycle.is_active == True)
        )).scalar_one_or_none()
        micro_id = active_micro.id if active_micro else None

    # === РАСЧЕТ ЦЕЛЕВОГО ОБЪЕМА (SNAPSHOT) ===
    calculated_targets = None
    if split_day_tag and not payload.plan_id:
        # TODO в будущем: передавать сюда meso_tag и micro_tag из календаря,
        # чтобы VolumeService мог применить коэффициенты RIR и усталости!
        calculated_targets = await VolumeService.calculate_session_targets(
            session=session,
            app_user_id=app_user.id,
            day_tag=split_day_tag
        )

    # --- СОЗДАЕМ СЕССИЮ ---
    workout = WorkoutSession(
        app_user_id=app_user.id,

        # ИСПРАВЛЕНИЕ: База ждет слово "split_day", а не "by_parameters"
        source="split_day" if payload.source == "by_parameters" else "free",

        status="active",
        split_day_id=payload.split_day_id if payload.source == "by_parameters" else None,
        plan_id=payload.plan_id,

        # calendar_day_id=payload.calendar_day_id, # (если добавил колонку в БД)

        app_user_mesocycle_id=meso_id,
        mesocycle_phase=current_phase,
        app_user_microcycle_id=micro_id,
        volume_targets=calculated_targets,
        notes=None,
    )

    session.add(workout)
    await session.flush()

    # --- РАСПАКОВКА ПЛАНА ЧЕРЕЗ DUP-ДВИЖОК (Если есть план) ---
    if payload.plan_id:
        compiled_exercises = await calculate_exercise_recommendations(session, app_user.id, plan_id=payload.plan_id)

        for ex_data in compiled_exercises:
            new_ex = WorkoutSessionExercise(
                workout_session_id=workout.id,
                exercise_id=ex_data["exercise_id"],
                order_index=ex_data["order_index"],
                superset_group=ex_data.get("superset_group"),
                recommended_rir=ex_data.get("recommended_rir"),
                recommended_rep_min=ex_data.get("recommended_rep_min"),
                recommended_rep_max=ex_data.get("recommended_rep_max")
            )
            session.add(new_ex)
            await session.flush()

            for i in range(ex_data["target_sets"]):
                new_set = WorkoutSessionSet(
                    workout_session_exercise_id=new_ex.id,
                    set_number=i + 1,
                    set_type="normal",
                    is_completed=False
                )
                session.add(new_set)

    await session.commit()
    await session.refresh(workout)

    return StartWorkoutResponse(
        id=workout.id,
        started_at=workout.started_at,
        source=workout.source,
    )


@router.post(
    "/workouts/{workout_id}/finish",
    response_model=FinishWorkoutResponse,
)
async def finish_workout(
        workout_id: int,
        db: AsyncSession = Depends(get_db),
        current_app_user=Depends(get_current_app_user),
):
    # Код завершения тренировки остался без изменений, так как он завязан
    # только на WorkoutSession и WorkoutSessionExercise
    stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.id == workout_id,
            WorkoutSession.app_user_id == current_app_user.id,
            WorkoutSession.status == "active",
        )
        .options(
            selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.exercise),
            selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.sets),
        )
    )

    result = await db.execute(stmt)
    workout = result.scalar_one_or_none()

    if workout is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Active workout not found",
        )

    finished_at = datetime.now(timezone.utc)
    workout.status = "finished"
    workout.finished_at = finished_at

    await db.commit()
    await db.refresh(workout)

    detail_stmt = (
        select(WorkoutSession)
        .where(WorkoutSession.id == workout_id)
        .options(
            selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.exercise),
            selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.sets),
        )
    )
    detail_result = await db.execute(detail_stmt)
    workout = detail_result.scalar_one()

    total_sets = 0
    total_reps = 0
    total_volume = Decimal("0")
    exercise_summaries: list[WorkoutFinishedExerciseSummary] = []

    for session_exercise in workout.exercises:
        exercise_sets = [s for s in session_exercise.sets if s.is_completed]

        sets_count = len(exercise_sets)
        reps_sum = sum(s.reps or 0 for s in exercise_sets)

        exercise_volume = Decimal("0")
        for s in exercise_sets:
            if s.weight is not None and s.reps is not None:
                exercise_volume += Decimal(s.weight) * Decimal(s.reps)

        total_sets += sets_count
        total_reps += reps_sum
        total_volume += exercise_volume

        exercise_summaries.append(
            WorkoutFinishedExerciseSummary(
                exercise_id=session_exercise.exercise.id,
                exercise_name=session_exercise.exercise.name,
                sets_count=sets_count,
                total_reps=reps_sum,
                total_volume=exercise_volume,
            )
        )

    duration_seconds = int((workout.finished_at - workout.started_at).total_seconds())

    return FinishWorkoutResponse(
        workout_id=workout.id,
        source=workout.source,
        started_at=workout.started_at,
        finished_at=workout.finished_at,
        duration_seconds=duration_seconds,
        exercises_count=len(workout.exercises),
        sets_count=total_sets,
        total_reps=total_reps,
        total_volume=total_volume,
        exercises=exercise_summaries,
    )

@router.patch("/workout-center/context/mesocycle", response_model=WorkoutCenterContextRead)
async def update_workout_center_mesocycle(
        payload: UpdateMesocycleContextPayload,
        session: AsyncSession = Depends(get_db),
        app_user: AppUser = Depends(get_current_app_user)
):
    # 1. Деактивируем все предыдущие стратегии
    await session.execute(
        update(AppUserMesocycle)
        .where(AppUserMesocycle.app_user_id == app_user.id)
        .values(is_active=False)
    )

    # 2. Создаем новую активную запись с кастомной длиной
    if payload.mesocycle_id is not None:
        new_active = AppUserMesocycle(
            app_user_id=app_user.id,
            mesocycle_id=payload.mesocycle_id,
            is_active=True,
            # ТЕПЕРЬ ДЛИНА ДИНАМИЧЕСКАЯ
            microcycle_length=payload.microcycle_length
        )
        session.add(new_active)

    await session.commit()
    return await build_context(session, app_user)


# --- ЭНДПОИНТ ПЛАНА ---
@router.patch("/workout-center/context/plan", response_model=WorkoutCenterContextRead)
async def update_workout_center_plan(
        payload: UpdatePlanContextPayload,
        session: AsyncSession = Depends(get_db),
        app_user: AppUser = Depends(get_current_app_user)
):
    # План привязывается к конкретному дню в активном сплите
    stmt = select(UserSplit).where(
        UserSplit.app_user_id == app_user.id,
        UserSplit.is_active == True,
    )
    result = await session.execute(stmt)
    user_split = result.scalar_one_or_none()

    if not user_split:
        raise HTTPException(status_code=400, detail="Сплит не выбран. Невозможно привязать план.")

    current_day_str = str(user_split.current_day)

    # Копируем словарь JSONB, иначе SQLAlchemy может не заметить изменений
    new_selected_plans = dict(user_split.selected_plans or {})

    if payload.plan_id is None:
        # Если пришел null, открепляем план от этого дня
        new_selected_plans.pop(current_day_str, None)
    else:
        # Прикрепляем новый план
        new_selected_plans[current_day_str] = payload.plan_id

    # Перезаписываем словарь, чтобы триггернуть update в БД
    user_split.selected_plans = new_selected_plans

    await session.commit()
    return await build_context(session, app_user)

@router.post("/workout-center/active-mesocycle/phase")
async def set_active_mesocycle_phase(
    payload: UpdateMesocyclePhasePayload,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user)
):
    active_meso_stmt = (
        select(AppUserMesocycle)
        .options(joinedload(AppUserMesocycle.mesocycle))
        .where(
            AppUserMesocycle.app_user_id == app_user.id,
            AppUserMesocycle.is_active == True
        )
    )
    result = await session.execute(active_meso_stmt)
    active_meso = result.scalar_one_or_none()

    if not active_meso:
        raise HTTPException(status_code=400, detail="Нет активного мезоцикла")

    # Теперь это отработает без ошибок, так как mesocycle уже загружен в память
    if payload.phase < 1 or payload.phase > active_meso.mesocycle.phases_in_cycle:
        raise HTTPException(status_code=400, detail="Неверный номер недели")

    active_meso.current_phase = payload.phase
    await session.commit()

    return await build_context(session, app_user)


@router.get("/workout-center/plan-builder/volume-targets", response_model=VolumeTargetsResponse)
async def get_volume_targets(
        day_tag: str = Query(..., description="Тег дня, например 'upper' или 'push'"),
        session: AsyncSession = Depends(get_db),
        app_user=Depends(get_current_app_user)
):
    # ШАГ 1: Достаем профиль юзера для получения volume_budget
    profile_stmt = select(AppUserProfile).where(AppUserProfile.app_user_id == app_user.id)
    profile_res = await session.execute(profile_stmt)
    profile = profile_res.scalar_one_or_none()

    if not profile or not profile.volume_budget:
        raise HTTPException(status_code=400, detail="Бюджет подходов (volume_budget) не настроен.")

    volume_budget = profile.volume_budget
    constraints = volume_budget.get("constraints", {})
    weekly_targets = volume_budget.get("weekly_targets", {})

    max_session_cap = constraints.get("max_sets_per_session_per_muscle", 10)

    # ШАГ 2: Ищем активный сплит юзера и подгружаем всю иерархию (Сплит -> Слоты -> Дни -> Мышцы)
    split_stmt = (
        select(SplitBlueprint)
        .join(UserSplit, UserSplit.blueprint_id == SplitBlueprint.id)
        .where(UserSplit.app_user_id == app_user.id, UserSplit.is_active == True)
        .options(
            selectinload(SplitBlueprint.slots)
            .selectinload(SplitDaySlot.day)
            .selectinload(DayBlueprint.muscle_targets)
        )
    )
    split_res = await session.execute(split_stmt)
    blueprint = split_res.scalar_one_or_none()

    if not blueprint:
        raise HTTPException(status_code=400, detail="Активный сплит не найден.")

    # ШАГ 3: Считаем частоту каждой мышцы в сплите и находим целевые мышцы для запрошенного дня
    muscle_frequencies = {}
    muscles_in_day = set()

    for slot in blueprint.slots:
        day = slot.day

        # Достаем значение из Enum (например, 'upper' или 'push')
        template_val = day.template_type.value if hasattr(day.template_type, 'value') else str(day.template_type)

        # Бронебойная проверка: совпадает либо имя ("Upper"), либо системный тип ("upper")
        is_target_day = (day.name.lower() == day_tag.lower() or template_val.lower() == day_tag.lower())

        for target in day.muscle_targets:
            muscle = target.muscle_group_id.lower()

            muscle_frequencies[muscle] = muscle_frequencies.get(muscle, 0) + 1

            if is_target_day:
                muscles_in_day.add(muscle)

    if not muscles_in_day:
        # Если по тегу ничего не нашли, отдаем пустой результат, чтобы фронт не упал
        return VolumeTargetsResponse(day_tag=day_tag, split_duration=blueprint.length_days, targets={})

    targets_response = {}

    # ШАГ 4, 5, 6: Применяем evidence-based математику
    for muscle in muscles_in_day:
        muscle_data = weekly_targets.get(muscle)
        if not muscle_data:
            continue

        target_weekly_sets = muscle_data.get("target_sets", 0)
        min_floor = muscle_data.get("min_floor", 2)
        frequency = muscle_frequencies.get(muscle, 1)  # На всякий случай защита от / 0

        if target_weekly_sets == 0 or frequency == 0:
            continue

        # Формула: (Недельный объем * (Длина сплита / 7)) / Частота активации
        raw_session_target = (target_weekly_sets * (blueprint.length_days / 7.0)) / frequency

        rounded_target = round(raw_session_target)

        # Валидация: не меньше min_floor и не больше max_session_cap
        final_target = max(min_floor, min(max_session_cap, rounded_target))

        targets_response[muscle] = MuscleTarget(
            target_sets=final_target,
            max_session_cap=max_session_cap
        )

    return VolumeTargetsResponse(
        day_tag=day_tag,
        split_duration=blueprint.length_days,
        targets=targets_response
    )


@router.patch("/workout-center/context/microcycle", response_model=WorkoutCenterContextRead)
async def update_workout_center_microcycle(
        payload: UpdateMicrocycleContextPayload,
        session: AsyncSession = Depends(get_db),
        app_user: AppUser = Depends(get_current_app_user)
):
    """Переключение активного микроцикла пользователя (поддерживает null для отвязки)."""

    # 1. Снимаем флаг активности со всех микроциклов данного пользователя
    await session.execute(
        update(AppUserMicrocycle)
        .where(AppUserMicrocycle.app_user_id == app_user.id)
        .values(is_active=False)
    )

    # 2. Если передан конкретный ID, делаем активным его
    if payload.microcycle_id is not None:
        await session.execute(
            update(AppUserMicrocycle)
            .where(
                AppUserMicrocycle.id == payload.microcycle_id,
                AppUserMicrocycle.app_user_id == app_user.id
            )
            .values(is_active=True)
        )

    await session.commit()

    # Возвращаем обновленный контекст для мгновенной синхронизации UI
    return await build_context(session, app_user)