from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import func, select, update
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
from api.services.progression import params as progression_params
from api.services.progression import repository as progression_repo
from api.services.progression.engine import plan_exercise
from api.services.progression.resolve import override_for
from api.services.readiness import repository as readiness_repo

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
    WorkoutSessionSet, UserCalendarDay, Exercise,
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
    # P0-09: пропуски проставляются лениво, при обращении. guarded()
    # изолирует падение в SAVEPOINT — голого try/except мало: работа идёт в
    # той же сессии, которую эндпоинт потом коммитит, и ошибка уровня DBAPI
    # уронила бы commit через PendingRollbackError, хотя исключение уже
    # поймано. Без контекста дня пользователь не начнёт тренировку.
    from api.services.volume.repository import guarded, mark_missed_days, utc_today

    await guarded(
        session,
        "простановка пропущенных дней",
        mark_missed_days(session, app_user.id, utc_today()),
    )

    # P0-09, Задача 11: обзор объёма материализуется лениво здесь же — по
    # тому же принципу «тихо доделать при обращении», что и пропуски выше.
    # guarded(), а не голый try/except — по той же причине (см. её докстринг):
    # падение решателя не должно отравить сессию, которую build_context
    # делит с остальной сборкой контекста.
    from api.services.volume.service import refresh_volume_proposals

    await guarded(
        session,
        "обновление обзора объёма",
        refresh_volume_proposals(session, app_user.id, utc_today()),
    )

    # P0-12: автопилот цели просыпается на той же точке — окно объёма уже
    # закрыто вызовом выше, значит есть новый факт и новый тренд.
    # refresh_goal_proposals коммитит СЕБЯ САМА на каждом пути записи (см. её
    # докстринг, финальное ревью Important 9). Это единственный commit во
    # всём build_context — на путях без ведущей цели (или без записи) именно
    # он, последний в цепочке, делает долговечными mark_missed_days и
    # refresh_volume_proposals выше — осознанно, тем же приёмом, что и
    # periodization.service.refresh_proposals. (Ревью Задачи 8, P1-03 ч.1:
    # раньше здесь же в начале функции стоял ещё и ensure_structure со своим
    # commit — теперь он переехал в обработчик GET /workout-center/context,
    # см. его докстринг; build_context сам по себе больше ничего, кроме этого
    # пути, не коммитит.)
    from api.services.goal.service import refresh_goal_proposals

    await guarded(
        session,
        "обновление автопилота цели",
        refresh_goal_proposals(session, app_user.id, utc_today()),
    )

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

    # P0-08, Задача 13: координата блока едет вместе с контекстом, чтобы
    # клиент не делал второй запрос ради одной строки на экране. Кнопка
    # переключения фазы (ниже) двигает start_date именно этого блока, а не
    # AppUserMesocycle.current_phase — второй источник правды рядом с
    # календарём (см. докстринг set_active_mesocycle_phase).
    from api.services.periodization.repository import get_active_block
    from api.services.periodization.service import block_coordinate as _block_coordinate

    active_block_out = None
    active_block = await get_active_block(session, app_user.id)
    if active_block is not None:
        active_block_out = await _block_coordinate(
            session, app_user.id, active_block, date.today()
        )

    # P0-08, Задача 13, ревью, Critical 1: AppUserMesocycle.current_phase
    # больше не источник правды после переключения фазы (переключатель
    # двигает start_date блока и не пишет это поле, см. докстринг
    # set_active_mesocycle_phase). Если активный блок есть, неделя и имя фазы
    # обязаны идти из его координаты (снимок фаз блока), а не из
    # current_phase — иначе один и тот же ответ отдавал бы новую фазу в
    # active_block.phase_number и замёрзшую старую в этих двух полях. Без
    # блока (периодизация не настроена) поведение остаётся прежним.
    if active_block_out is not None:
        selected_periodization_week = active_block_out.phase_number
        phase_label = active_block_out.phase_name

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
        active_block=active_block_out,
    )


@router.get("/workout-center/context", response_model=WorkoutCenterContextRead)
async def get_workout_center_context(
        app_user: AppUser = Depends(get_current_app_user),
        session: AsyncSession = Depends(get_db),
):
    # P1-03 ч.1, §5.5, ревью Задачи 8: ensure_structure стоит ЗДЕСЬ, в
    # обработчике конкретного эндпоинта, а не в build_context — хотя
    # build_context дёргают семь эндпоинтов. Смысл ensure_structure —
    # «дочинить структуру при открытии экрана тренировки» (пользователь с
    # активным сплитом, но без мезо и микро, не получил бы блока вовсе:
    # ensure_active_block вернул бы None, а вместе с ним не работали бы
    # P0-08, P0-09 и P0-12), а не «на любое обращение к контексту». Раньше
    # вызов сидел в начале build_context и это давало регрессию: PATCH
    # /workout-center/context/mesocycle с mesocycle_id=null — легальный
    # способ снять мезоцикл («Без мезоцикла» в селекторе мобильного клиента,
    # см. update_workout_center_mesocycle ниже) — деактивирует все записи,
    # коммитит и зовёт build_context за свежим контекстом. Сидя внутри
    # build_context, ensure_structure видела отсутствие активного мезоцикла и
    # тут же реактивировала дефолтный пресет — в ответе ТОГО ЖЕ запроса
    # пользователь получал мезоцикл обратно, хотя явно его снял. GET
    # /workout-center/context — единственный из семи эндпоинтов, который по
    # смыслу и есть «открытие экрана тренировки», поэтому вызов переехал
    # сюда одного.
    #
    # Вызов — первая строка обработчика, ДО build_context: ensure_structure
    # умеет откатить транзакцию сессии в ветке гонки (session.rollback() в
    # обработчике IntegrityError, см. её докстринг, тот же контракт, что и у
    # ensure_active_block в api/services/periodization/repository.py) — до
    # неё в этой сессии не должно быть незакоммиченных изменений, а в начале
    # обработчика их и нет.
    from api.services.structure.bootstrap import ensure_structure

    await ensure_structure(session, app_user.id)
    await session.commit()

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

    # Финальное ревью P1-03, Critical 1: это ВТОРОЙ вход смены сплита — тот
    # же blueprint_id меняется и в POST /splits/active (splits.py), и здесь.
    # Мобильный селектор на экране тренировки бьёт именно сюда, а не в
    # /splits/active. Без пересборки микроциклы остаются на длине старого
    # сплита, и scheduling_engine молча уводит раскладку повторов
    # относительно дней нового сплита — тот самый дефект, ради которого
    # затевалась вся эта задача. flush ДО вызова обязателен: rebuild_for_active_split
    # читает активный UserSplit тем же SELECT в этой же сессии, и без flush
    # он увидел бы старый blueprint_id.
    from api.services.structure.bootstrap import rebuild_for_active_split

    await session.flush()
    await rebuild_for_active_split(session, app_user.id)

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
    current_day_index = None
    training_block_id = None

    # 1. Если это тренировка из Календаря, берем ИДЕАЛЬНЫЕ данные от движка
    if payload.calendar_day_id:
        cal_stmt = select(UserCalendarDay).where(UserCalendarDay.id == payload.calendar_day_id)
        cal_day = (await session.execute(cal_stmt)).scalar_one_or_none()
        if cal_day:
            meso_id = cal_day.user_mesocycle_id
            current_phase = cal_day.mesocycle_phase_number
            micro_id = cal_day.user_microcycle_id
            # Реальный день внутри микроцикла (1, 2, 3...) — питает DUP-движок,
            # чтобы RIR/диапазон повторов считались по фактическому дню, а не по хардкоду.
            current_day_index = cal_day.microcycle_day_number

            training_block_id = cal_day.block_id

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

        # Свободная тренировка вне календаря всё равно принадлежит блоку:
        # иначе её предписания резолвили бы фазу по шаблону и разошлись бы
        # с вставленной разгрузкой.
        from api.services.periodization.repository import ensure_active_block

        active_block = await ensure_active_block(session, app_user.id, date.today())
        training_block_id = active_block.id if active_block else None

        # P0-08, Задача 13, ревью, Critical 1: если блок есть, фаза свободной
        # тренировки обязана идти из его координаты, а не из
        # AppUserMesocycle.current_phase — иначе после переключения фазы
        # training_block_id указывал бы на блок с одной координатой, а
        # mesocycle_phase нёс бы старую фазу. Дальше это рассогласование
        # кормит резолв уровня усилия (resolve_phase_effort_tier) и движок
        # прогрессии. Без блока (периодизация не настроена) поведение
        # остаётся прежним — current_phase из current_phase.
        if active_block is not None:
            from api.services.periodization.service import (
                block_coordinate as _block_coordinate,
            )

            coordinate = await _block_coordinate(
                session, app_user.id, active_block, date.today()
            )
            current_phase = coordinate.phase_number

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

        calendar_day_id=payload.calendar_day_id,

        app_user_mesocycle_id=meso_id,
        mesocycle_phase=current_phase,
        app_user_microcycle_id=micro_id,
        training_block_id=training_block_id,
        volume_targets=calculated_targets,
        notes=None,
    )

    session.add(workout)
    await session.flush()

    # --- РАСПАКОВКА ПЛАНА ЧЕРЕЗ DUP-ДВИЖОК (Если есть план) ---
    if payload.plan_id:
        compiled_exercises = await calculate_exercise_recommendations(
            session, app_user.id, plan_id=payload.plan_id, current_day_index=current_day_index
        )

        # P0-09: принятые пользователем правки объёма живут на дне
        # календаря (UserCalendarDay.volume_adjustments), а не в плане —
        # план переиспользуется на всех подходящих днях. Накладываем их
        # ДО построения контекста прогрессии, чтобы движок увидел
        # фактическое число подходов, а не шаблонное.
        if payload.calendar_day_id:
            from api.services.volume.measure import apply_adjustments

            adjustments_row = (await session.execute(
                select(UserCalendarDay.volume_adjustments).where(
                    UserCalendarDay.id == payload.calendar_day_id,
                    UserCalendarDay.app_user_id == app_user.id,
                )
            )).scalar_one_or_none()
            compiled_exercises = apply_adjustments(compiled_exercises, adjustments_row)

        # P0-06 C1: до этого фикса /workouts/start с plan_id создавал
        # WorkoutSessionExercise только со снимками recommended_*, ни разу не
        # вызывая движок. Из-за write-once в persist_prescription и
        # resolve._has_prescription_history это означало, что у пользователя,
        # тренирующегося по плану, prescription НИКОГДА не появлялся в
        # истории -> схема навсегда оставалась бутстрапом e1rm_factor.
        # Единственный до сих пор рабочий путь был add_exercise_to_workout
        # (api/routers/workouts.py) — свободное добавление упражнения.
        profile_result = await session.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == app_user.id)
        )
        profile = profile_result.scalars().first()
        experience_level = profile.experience_level if profile else None
        settings = profile.settings if profile else None

        # Вердикт один на всю сессию — резолвим до цикла, как и фазу
        # мезоцикла (тот же принцип, что и P0-06 C2: не плодить запросы).
        readiness_verdict = await readiness_repo.verdict_for_checkin(
            session, app_user.id, payload.readiness_checkin_uuid, settings
        )

        # Фаза мезоцикла одна на всю сессию — резолвим один раз до цикла,
        # а не на каждое упражнение (иначе N лишних запросов join'а поверх
        # уже существующего N+1 build_context по load_history, P0-06 C2).
        phase_effort_tier = await progression_repo.resolve_phase_effort_tier(
            session, workout.app_user_mesocycle_id, workout.mesocycle_phase,
            training_block_id=workout.training_block_id,
        )

        # Пачкой подгружаем упражнения плана — build_context нужен объект
        # Exercise (equipment_needed/fatigue_tier/main_muscle_group), а
        # ленивая подгрузка через session_exercise.exercise упадёт вне
        # greenlet-контекста в async SQLAlchemy. Один SELECT на всю сессию
        # вместо одного на упражнение — тот же принцип, что и резолв фазы.
        exercise_ids = [ex_data["exercise_id"] for ex_data in compiled_exercises]
        exercises_by_id: dict[int, Exercise] = {}
        if exercise_ids:
            ex_rows = await session.execute(
                select(Exercise).where(Exercise.id.in_(exercise_ids))
            )
            exercises_by_id = {e.id: e for e in ex_rows.scalars().all()}

        for ex_data in compiled_exercises:
            new_ex = WorkoutSessionExercise(
                workout_session_id=workout.id,
                exercise_id=ex_data["exercise_id"],
                order_index=ex_data["order_index"],
                superset_group=ex_data.get("superset_group"),
                recommended_rir=ex_data.get("recommended_rir"),
                recommended_rep_min=ex_data.get("recommended_rep_min"),
                recommended_rep_max=ex_data.get("recommended_rep_max"),
                target_sets=ex_data.get("target_sets")  # <--- СОХРАНЯЕМ В БД ЗДЕСЬ
            )
            session.add(new_ex)
            await session.flush()

            new_ex.exercise = exercises_by_id.get(ex_data["exercise_id"])

            ctx = await progression_repo.build_context(
                session,
                new_ex,
                app_user.id,
                experience_level,
                settings,
                rep_range_source=ex_data.get(
                    "rep_range_source", progression_params.REP_SOURCE_FALLBACK
                ),
                phase_effort_tier=phase_effort_tier,
                readiness=readiness_verdict,
            )
            prescription = plan_exercise(
                ctx, override=override_for(settings, new_ex.exercise_id)
            )
            progression_repo.persist_prescription(new_ex, prescription)

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

    # P0-09: календарь запоминает факт. guarded() изолирует падение в
    # SAVEPOINT — учёт adherence это надстройка, и она не имеет права
    # уронить завершение тренировки ни исключением, ни отравленной
    # транзакцией (см. её докстринг).
    from api.services.volume.repository import attach_session_to_day, guarded

    await guarded(
        db,
        "привязка сессии к дню календаря",
        attach_session_to_day(db, current_app_user.id, workout),
    )

    # Оценка результата и предварительное предписание на следующий раз.
    # next_prescription едет на устройство и делает прогрессию доступной
    # офлайн даже для упражнения, добавленного без сети.
    #
    # Порядок принципиален: цикл идёт ПОСЛЕ того, как статус тренировки стал
    # finished и проставлен finished_at (см. выше). load_history отбирает
    # сессии по WorkoutSession.status == "finished" — если пересчитать раньше,
    # текущая сессия в свою же историю не попадёт и состояние обновится по
    # устаревшим данным. Автофлаш AsyncSession перед execute() гарантирует,
    # что build_context увидит эти изменения ещё до commit.
    profile_result = await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_app_user.id)
    )
    profile = profile_result.scalars().first()
    experience_level = profile.experience_level if profile else None
    settings = profile.settings if profile else None

    # P0-06 C2: фаза мезоцикла одна на всю сессию (WorkoutSession.mesocycle_phase),
    # резолвим ОДИН раз до цикла — иначе на каждое упражнение сессии пришёлся бы
    # свой запрос join'а, что усугубило бы уже существующий N+1 build_context
    # (по load_history на упражнение).
    phase_effort_tier = await progression_repo.resolve_phase_effort_tier(
        db, workout.app_user_mesocycle_id, workout.mesocycle_phase,
        training_block_id=workout.training_block_id,
    )

    for se in workout.exercises:
        ctx = await progression_repo.build_context(
            db, se, current_app_user.id, experience_level, settings,
            phase_effort_tier=phase_effort_tier,
        )
        nxt = plan_exercise(
            ctx,
            override=override_for(settings, se.exercise_id),
            provisional=True,
        )
        await progression_repo.refresh_state(
            db, current_app_user.id, se.exercise_id, nxt
        )

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

    # P0-08, Задача 13, Шаг 6 брифа: свежая сессия меняет и усталость, и
    # картину плато — пересчитываем предложения сразу, чтобы карточка
    # появилась к следующему открытию Home. safe_*, а не прямой вызов:
    # завершение тренировки — самый дорогой путь для отказа, и падение
    # решателя не должно стоить пользователю залогированной сессии (функция
    # обёрнута в SAVEPOINT, см. её докстринг — падение решателя откатывает
    # только его собственную работу, а не уже закоммиченное завершение
    # тренировки выше).
    from api.services.periodization.service import safe_refresh_proposals

    await safe_refresh_proposals(db, current_app_user.id)

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
    """Ручной переезд на другую фазу активного БЛОКА (P0-08, Задача 13).

    Раньше писался AppUserMesocycle.current_phase — второй источник правды
    рядом с календарём (снимком фаз блока). С появлением блока их стало бы
    три, и они гарантированно разъехались бы. Теперь двигается start_date
    блока так, чтобы сегодня стало первым днём выбранной фазы, а будущее
    перегенерируется. Поле current_phase остаётся ради обратной совместимости
    старых сессий (см. WorkoutSession.mesocycle_phase и
    calculate_exercise_recommendation в ветке без календаря), но источником
    правды уже не является.

    Оговорка P0-09: «сегодня стало первым днём выбранной фазы» — не
    безусловное обещание. _regenerate_future (include_today=True) не
    трогает сегодняшний день, если он уже несёт факт (статус
    completed/missed) или принятую пользователем правку предписания
    (непустой volume_adjustments) — такой день остаётся на прежней
    координате фазы. Сохранить факт важнее, чем перерисовать уже
    отработанный или уже настроенный день под новую фазу.

    Поправка 3 брифа Задачи 13: offset_days обязан считать длину ТОЛЬКО фаз
    ПЕРЕД целевой (цикл ниже прерывается до прибавления её собственной
    длины) — если бы в offset_days попала ещё и сама целевая фаза, start_date
    уехал бы на один шаг дальше в прошлое, чем нужно, а planned_end_date
    (start_date + сумма ВСЕХ длин блока - 1) мог бы оказаться РАНЬШЕ
    сегодняшнего дня. Тогда первый же вызов ensure_active_block (из
    /workouts/start или построения календаря) увидел бы today > planned_end_date
    и немедленно закрыл бы блок автопереходом — пользователь нажал «перейти
    на фазу N», а получил новый блок вместо перемещения по текущему. С
    offset_days, считающим строго ДО целевой фазы, planned_end_date всегда
    покрывает today: сумма длин ВСЕХ фаз минус offset_days (то есть длина
    целевой фазы и всех фаз после неё) не может быть меньше длины самой
    целевой фазы, а значит planned_end_date = today + (эта сумма - 1) >= today.
    Тест test_phase_switch_keeps_block_alive_for_ensure_active_block в
    tests/integration/test_periodization_phase_switch.py проверяет это явно.
    """
    from datetime import date as date_cls, timedelta

    from api.services.periodization.phases import from_json
    from api.services.periodization.repository import get_active_block
    from api.services.periodization.service import _regenerate_future

    today = date_cls.today()
    block = await get_active_block(session, app_user.id)
    if block is None:
        raise HTTPException(status_code=404, detail="Активный блок не найден")

    snapshot = from_json(block.phases)
    offset_days = 0
    target = None
    for phase in snapshot:
        if phase.phase_number == payload.phase:
            target = phase
            break
        offset_days += phase.length_days

    if target is None:
        raise HTTPException(status_code=400, detail="Такой фазы нет в текущем блоке")

    block.start_date = today - timedelta(days=offset_days)
    total = sum(p.length_days for p in snapshot)
    block.planned_end_date = block.start_date + timedelta(days=total - 1)

    # P0-08, Задача 13, ревью, Critical 1: зеркалим current_phase в
    # AppUserMesocycle следом за координатой блока. Поле оставлено ради
    # сессий, созданных до P0-08 (см. докстринг выше), источником правды
    # больше не является — но и расходиться с блоком не должно: это дёшево
    # и страхует любых читателей current_phase, которых мы могли не найти.
    active_meso_stmt = select(AppUserMesocycle).where(
        AppUserMesocycle.app_user_id == app_user.id,
        AppUserMesocycle.is_active == True,
    )
    active_meso = (await session.execute(active_meso_stmt)).scalar_one_or_none()
    if active_meso is not None:
        active_meso.current_phase = payload.phase

    await session.flush()
    # P0-08, Задача 13, ревью, Critical 2: include_today=True — см. докстринг
    # _regenerate_future. Только этот путь (прямая команда пользователя
    # «перейти на фазу N») имеет право переписать сегодняшний день календаря;
    # insert_deload/postpone/close_block по-прежнему трогают только будущее.
    await _regenerate_future(session, app_user.id, block, today, include_today=True)
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
        return VolumeTargetsResponse(day_tag=day_tag, split_duration=blueprint.length_days, targets={}, experience_level=profile.experience_level)

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
        cycle_share = blueprint.length_days / 7.0
        raw_session_target = (target_weekly_sets * cycle_share) / frequency
        raw_session_floor = (min_floor * cycle_share) / frequency

        rounded_target = round(raw_session_target)
        rounded_floor = round(raw_session_floor)

        # Ограничиваем пересчитанным минимумом на сессию и сессионным максимумом.
        final_target = max(rounded_floor, min(max_session_cap, rounded_target))

        targets_response[muscle] = MuscleTarget(
            target_sets=final_target,
            max_session_cap=max_session_cap
        )

    return VolumeTargetsResponse(
        day_tag=day_tag,
        split_duration=blueprint.length_days,
        targets=targets_response,
        experience_level=profile.experience_level,
    )


@router.patch("/workout-center/context/microcycle", response_model=WorkoutCenterContextRead)
async def update_workout_center_microcycle(
        payload: UpdateMicrocycleContextPayload,
        session: AsyncSession = Depends(get_db),
        app_user: AppUser = Depends(get_current_app_user)
):
    """Переключение активного микроцикла пользователя (поддерживает null для отвязки)."""

    # P1-03 ч.1, §5.4: длина микроцикла обязана совпадать с числом слотов
    # активного сплита. SchedulingEngine считает день сплита и день микроцикла
    # двумя независимыми модулями по одному счётчику, и при расхождении длин
    # раскладка повторов уезжает относительно дней — молча и накопительно.
    # Подстраивать одну сторону нельзя: days_mapping человек мог составить
    # руками.
    if payload.microcycle_id is not None:
        micro = (await session.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.id == payload.microcycle_id,
                AppUserMicrocycle.app_user_id == app_user.id,
            )
        )).scalars().first()
        if micro is None:
            raise HTTPException(404, "Микроцикл не найден")

        active_split = (await session.execute(
            select(UserSplit).where(
                UserSplit.app_user_id == app_user.id,
                UserSplit.is_active.is_(True),
            )
        )).scalars().first()

        if active_split is not None:
            slot_count = (await session.execute(
                select(func.count(SplitDaySlot.id))
                .where(SplitDaySlot.blueprint_id == active_split.blueprint_id)
            )).scalar_one()
            if slot_count and micro.length_days != slot_count:
                raise HTTPException(
                    409,
                    f"Микроцикл рассчитан на {micro.length_days} дн., "
                    f"а активный сплит — на {slot_count}. "
                    f"Перестройте микроцикл под сплит.",
                )

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
