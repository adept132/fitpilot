import uuid
from datetime import timedelta, datetime, time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, or_
from sqlalchemy.orm import selectinload
from typing import List
from uuid import UUID
from starlette import status

from api.services.app_user_service import get_current_app_user
# Импортируй свои зависимости (пути могут немного отличаться в зависимости от твоего проекта)
from api.services.models import (
    AppUser, SplitBlueprint, DayBlueprint, SplitDaySlot, UserSplit, DayMuscleTarget, UserCalendarDay, AppUserMesocycle,
    AppUserMicrocycle
)
from api.schemas.splits import SplitBlueprintOut, DayBlueprintOut, ActivateSplitRequest, CreateCustomSplitRequest, \
    UpdateCustomSplitRequest, CreateCustomDayRequest, UpdateCustomDayRequest, SchedulePreviewRequest, \
    SchedulePreviewResponse, ScheduleLaunchRequest
from api.services.scheduling_engine import SchedulingEngine
from app.database import get_session

router = APIRouter(prefix="/splits", tags=["Splits Workspace"])


@router.get("/blueprints", response_model=List[SplitBlueprintOut])
async def get_available_splits(
        session: AsyncSession = Depends(get_session),
        current_user: AppUser = Depends(get_current_app_user)
):
    """
    Отдает библиотеку сплитов: Системные + Кастомные (созданные этим юзером).
    """
    stmt = (
        select(SplitBlueprint)
        .where(
            (SplitBlueprint.is_system == True) |
            (SplitBlueprint.author_id == current_user.id)
        )
        # Подгружаем все вложенные связи одним супер-запросом
        .options(
            selectinload(SplitBlueprint.slots)
            .selectinload(SplitDaySlot.day)
            .selectinload(DayBlueprint.muscle_targets)
        )
        .order_by(SplitBlueprint.is_system.desc(), SplitBlueprint.name)
    )
    result = await session.execute(stmt)
    splits = result.scalars().all()

    return splits


@router.get("/days", response_model=List[DayBlueprintOut])
async def get_available_days(
        session: AsyncSession = Depends(get_session),
        current_user: AppUser = Depends(get_current_app_user)
):
    """
    Отдает библиотеку "Кубиков" для карусели в песочнице.
    """
    stmt = (
        select(DayBlueprint)
        .where(
            (DayBlueprint.is_system == True) |
            (DayBlueprint.author_id == current_user.id)
        )
        .options(selectinload(DayBlueprint.muscle_targets))
        .order_by(DayBlueprint.is_system.desc(), DayBlueprint.name)
    )
    result = await session.execute(stmt)
    days = result.scalars().all()

    return days


@router.post("/active")
async def activate_split(
        payload: ActivateSplitRequest,
        session: AsyncSession = Depends(get_session),
        current_user: AppUser = Depends(get_current_app_user)
):
    """
    Устанавливает выбранный сплит как активную программу пользователя.
    """
    # 1. Проверяем, существует ли такой чертеж
    blueprint = await session.get(SplitBlueprint, payload.blueprint_id)
    if not blueprint:
        raise HTTPException(status_code=404, detail="Split blueprint not found")

    # 2. Деактивируем предыдущий активный сплит (если есть)
    await session.execute(
        update(UserSplit)
        .where(UserSplit.app_user_id == current_user.id)
        .where(UserSplit.is_active == True)
        .values(is_active=False)
    )

    # 3. Создаем новую активную сессию
    new_user_split = UserSplit(
        app_user_id=current_user.id,
        blueprint_id=blueprint.id,
        is_active=True,
        current_day=1  # Начинаем с первого дня цикла
    )

    session.add(new_user_split)
    await session.commit()

    return {"status": "success", "message": f"Split '{blueprint.name}' activated."}

@router.post("/custom")
async def create_custom_split(
    payload: CreateCustomSplitRequest,
    session: AsyncSession = Depends(get_session),
    current_user: AppUser = Depends(get_current_app_user)
):
    """
    Создает пользовательский сплит и расставляет дни по слотам.
    """
    # 1. Находим системный кубик отдыха (для заполнения null слотов)
    system_days_stmt = select(DayBlueprint).where(DayBlueprint.is_system == True).options(
        selectinload(DayBlueprint.muscle_targets))
    system_days_result = await session.execute(system_days_stmt)
    system_days = system_days_result.scalars().all()

    # Ищем день отдыха средствами Python: по названию или по отсутствию целевых мышц
    rest_day = next(
        (d for d in system_days if "отдых" in d.name.lower() or "rest" in d.name.lower() or len(d.muscle_targets) == 0),
        None
    )

    if not rest_day:
        raise HTTPException(status_code=500, detail="Системный день отдыха не найден в БД")

    # 2. Создаем "Каркас" (Split Blueprint)
    new_split = SplitBlueprint(
        name=payload.name,
        author_id=current_user.id,
        length_days=payload.length_days,
        is_system=False
    )
    session.add(new_split)
    await session.flush() # Получаем ID нового сплита без коммита транзакции

    # 3. Создаем слоты (Split Day Slots)
    for order, day_id in enumerate(payload.day_blueprint_ids):
        slot = SplitDaySlot(
            blueprint_id=new_split.id,
            day_order=order + 1,
            # Если day_id пришел null, ставим ID кубика отдыха
            day_id=day_id if day_id else rest_day.id
        )
        session.add(slot)

    await session.commit()
    return {"status": "success", "split_id": new_split.id, "message": "Сплит успешно сохранен"}


@router.delete("/{blueprint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_split(
        blueprint_id: UUID,
        session: AsyncSession = Depends(get_session),
        current_user: AppUser = Depends(get_current_app_user)
):
    """Удаляет пользовательский сплит."""
    stmt = select(SplitBlueprint).where(SplitBlueprint.id == blueprint_id)
    result = await session.execute(stmt)
    split = result.scalar_one_or_none()

    if not split:
        raise HTTPException(status_code=404, detail="Сплит не найден")

    if split.is_system or split.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Нельзя удалить этот сплит")

    await session.delete(split)
    await session.commit()
    return None


@router.patch("/{blueprint_id}")
async def update_custom_split(
        blueprint_id: UUID,
        payload: UpdateCustomSplitRequest,
        session: AsyncSession = Depends(get_session),
        current_user: AppUser = Depends(get_current_app_user)
):
    """Обновляет название или структуру пользовательского сплита."""
    stmt = select(SplitBlueprint).where(SplitBlueprint.id == blueprint_id)
    result = await session.execute(stmt)
    split = result.scalar_one_or_none()

    if not split:
        raise HTTPException(status_code=404, detail="Сплит не найден")

    if split.is_system or split.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Нельзя редактировать этот сплит")

    # 1. Обновляем базовые поля
    if payload.name is not None:
        split.name = payload.name
    if payload.length_days is not None:
        split.length_days = payload.length_days

    # 2. Обновляем структуру (слоты), если она пришла
    if payload.day_blueprint_ids is not None:
        # Ищем системный день отдыха
        rest_day_stmt = select(DayBlueprint).where(DayBlueprint.is_system == True).options(
            selectinload(DayBlueprint.muscle_targets))
        rest_days_result = await session.execute(rest_day_stmt)
        rest_day = next((d for d in rest_days_result.scalars() if
                         "отдых" in d.name.lower() or "rest" in d.name.lower() or len(d.muscle_targets) == 0), None)

        if not rest_day:
            raise HTTPException(status_code=500, detail="Системный день отдыха не найден")

        # Удаляем старые слоты
        await session.execute(delete(SplitDaySlot).where(SplitDaySlot.blueprint_id == split.id))

        # Создаем новые
        for order, day_id in enumerate(payload.day_blueprint_ids):
            slot = SplitDaySlot(
                blueprint_id=split.id,
                day_order=order + 1,
                day_id=day_id if day_id else rest_day.id
            )
            session.add(slot)

    await session.commit()
    return {"status": "success", "message": "Сплит обновлен"}


@router.post("/days/custom")
async def create_custom_day(
        payload: CreateCustomDayRequest,
        session: AsyncSession = Depends(get_session),
        current_user: AppUser = Depends(get_current_app_user)
):
    """Создает новый пользовательский кубик (день) для конструктора."""

    # 1. Создаем сам чертеж дня
    new_day = DayBlueprint(
        name=payload.name,
        template_type="custom",  # Помечаем как кастомный
        author_id=current_user.id,
        is_system=False
    )
    session.add(new_day)
    await session.flush()  # Получаем ID

    # 2. Привязываем к нему выбранные мышцы
    for muscle in payload.muscle_targets:
        session.add(DayMuscleTarget(
            day_id=new_day.id,
            muscle_group_id=muscle
        ))

    await session.commit()
    return {"status": "success", "day_id": new_day.id, "message": "День успешно создан"}


@router.delete("/days/{day_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_day(
        day_id: UUID,
        session: AsyncSession = Depends(get_session),
        current_user: AppUser = Depends(get_current_app_user)
):
    """Удаляет пользовательский тренировочный день."""
    stmt = select(DayBlueprint).where(DayBlueprint.id == day_id)
    result = await session.execute(stmt)
    day = result.scalar_one_or_none()

    if not day:
        raise HTTPException(status_code=404, detail="День не найден")

    if day.is_system or day.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Нельзя удалить этот день")

    await session.delete(day)
    await session.commit()
    return None


@router.patch("/days/{day_id}")
async def update_custom_day(
        day_id: UUID,
        payload: UpdateCustomDayRequest,
        session: AsyncSession = Depends(get_session),
        current_user: AppUser = Depends(get_current_app_user)
):
    """Обновляет название или мышцы пользовательского дня."""
    stmt = select(DayBlueprint).where(DayBlueprint.id == day_id)
    result = await session.execute(stmt)
    day = result.scalar_one_or_none()

    if not day:
        raise HTTPException(status_code=404, detail="День не найден")

    if day.is_system or day.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Нельзя редактировать этот день")

    # Обновляем имя, если пришло
    if payload.name is not None:
        day.name = payload.name

    # Обновляем мышцы, если пришли (удаляем старые, пишем новые)
    if payload.muscle_targets is not None:
        await session.execute(delete(DayMuscleTarget).where(DayMuscleTarget.day_id == day.id))
        for muscle in payload.muscle_targets:
            session.add(DayMuscleTarget(day_id=day.id, muscle_group_id=muscle))

    await session.commit()
    return {"status": "success", "message": "День обновлен"}


@router.post("/launch", status_code=status.HTTP_200_OK)
async def launch_split(
        request: ScheduleLaunchRequest,
        session: AsyncSession = Depends(get_session),
        current_user: AppUser = Depends(get_current_app_user)
):
    # 1. Проверяем существование чертежа сплита (ОБЯЗАТЕЛЬНАЯ СУЩНОСТЬ)
    query = select(SplitBlueprint).where(
        SplitBlueprint.id == request.blueprint_id,
        or_(
            SplitBlueprint.author_id == current_user.id,
            SplitBlueprint.is_system == True
        )
    )
    result = await session.execute(query)
    blueprint = result.scalar_one_or_none()

    if not blueprint:
        raise HTTPException(status_code=404, detail="Сплит не найден")

    # 2. Деактивируем все предыдущие активные сплиты пользователя
    deactivate_query = (
        update(UserSplit)
        .where(
            UserSplit.app_user_id == current_user.id,
            UserSplit.is_active == True
        )
        .values(is_active=False)
    )
    await session.execute(deactivate_query)

    # 3. Сохраняем забаненные дни в JSONB
    user_settings = {
        "blackout_weekdays": request.blackout_weekdays
    }

    start_datetime = datetime.combine(request.start_date, time.min)

    # 4. Создаем новую запись активного сплита
    new_user_split = UserSplit(
        app_user_id=current_user.id,
        blueprint_id=blueprint.id,
        start_date=start_datetime,
        is_active=True,
        current_day=1,
        selected_plans=user_settings,
        last_trained_date=None
    )
    session.add(new_user_split)

    # 5. Очищаем будущее расписание в UserCalendarDay
    delete_stmt = delete(UserCalendarDay).where(
        UserCalendarDay.app_user_id == current_user.id,
        UserCalendarDay.target_date >= request.start_date
    )
    await session.execute(delete_stmt)
    await session.commit()

    # 6. ПРОВЕРЯЕМ ОПЦИОНАЛЬНЫЙ МЕЗОЦИКЛ
    meso_stmt = select(AppUserMesocycle).where(
        AppUserMesocycle.app_user_id == current_user.id,
        AppUserMesocycle.is_active == True
    )
    meso_res = await session.execute(meso_stmt)
    user_meso = meso_res.scalar_one_or_none()
    user_meso_id = user_meso.id if user_meso else None

    # 7. ЗАПУСКАЕМ ПЕЧАТНЫЙ СТАНОК
    try:
        await SchedulingEngine.launch_and_unroll_plan(
            session=session,
            app_user_id=current_user.id,
            split_blueprint_id=blueprint.id,
            start_date=request.start_date,
            blackout_weekdays=request.blackout_weekdays,
            user_mesocycle_id=user_meso_id,
            preview_length_days=90
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка генерации расписания: {str(e)}")

    return {
        "status": "success",
        "message": f"Сплит '{blueprint.name}' успешно активирован и развернут в календарь!"
    }

@router.get("/active")
async def get_active_split(
    session: AsyncSession = Depends(get_session),
    current_user = Depends(get_current_app_user)
):
    # Ищем активный UserSplit и сразу подтягиваем его Blueprint
    query = (
        select(UserSplit)
        .options(selectinload(UserSplit.blueprint))
        .where(
            UserSplit.app_user_id == current_user.id,
            UserSplit.is_active == True
        )
    )
    result = await session.execute(query)
    active_split = result.scalar_one_or_none()

    if not active_split:
        return None # Сплит не назначен

    # Достаем забаненные дни из JSONB
    blackouts = []
    if active_split.selected_plans and "blackout_weekdays" in active_split.selected_plans:
        blackouts = active_split.selected_plans["blackout_weekdays"]

    return {
        "id": active_split.id,
        "blueprint_id": active_split.blueprint_id,
        "blueprint_name": active_split.blueprint.name,
        # Отдаем дату старта в виде строки YYYY-MM-DD
        "start_date": active_split.start_date.strftime("%Y-%m-%d"),
        "current_day": active_split.current_day,
        "blackout_weekdays": blackouts
    }

@router.get("/{blueprint_id}", response_model=SplitBlueprintOut)
async def get_custom_split_details(
        blueprint_id: UUID,
        session: AsyncSession = Depends(get_session),
        current_user: AppUser = Depends(get_current_app_user)
):
    """Возвращает полную структуру конкретного сплита для его редактирования."""
    stmt = (
        select(SplitBlueprint)
        .where(
            SplitBlueprint.id == blueprint_id,
            (SplitBlueprint.is_system == True) | (SplitBlueprint.author_id == current_user.id)
        )
        .options(
            selectinload(SplitBlueprint.slots)
            .selectinload(SplitDaySlot.day)
            .selectinload(DayBlueprint.muscle_targets)
        )
    )
    result = await session.execute(stmt)
    split = result.scalar_one_or_none()

    if not split:
        raise HTTPException(status_code=404, detail="Сплит не найден")

    return split


@router.post("/preview", response_model=SchedulePreviewResponse)
async def generate_schedule_preview(
        request: SchedulePreviewRequest,
        session: AsyncSession = Depends(get_session),
        current_user=Depends(get_current_app_user)
):
    # 1. Загружаем структуру сплита
    query = (
        select(SplitBlueprint)
        .where(
            SplitBlueprint.id == request.blueprint_id,
            or_(
                SplitBlueprint.author_id == current_user.id,
                SplitBlueprint.is_system == True
            )
        )
        .options(
            selectinload(SplitBlueprint.slots)
            .selectinload(SplitDaySlot.day)
            .selectinload(DayBlueprint.muscle_targets)  # <--- ДОБАВИЛИ ЖАДНУЮ ЗАГРУЗКУ ЦЕЛЕЙ
        )
    )
    result = await session.execute(query)
    split = result.scalar_one_or_none()

    if not split or not split.slots:
        raise HTTPException(status_code=404, detail="Сплит пуст или не найден")

    # Сортируем слоты по порядку day_order
    slots_queue = sorted(split.slots, key=lambda s: s.day_order)
    queue_length = len(slots_queue)

    # 2. Определяем стартовый индекс в очереди
    # Если фронтенд передал конкретный start_slot_id — начинаем с него.
    # Если нет — берем самый первый слот из очереди.
    slot_index = 0
    chosen_start_slot_id = getattr(request, 'start_slot_id', None)

    if chosen_start_slot_id:
        for idx, slot in enumerate(slots_queue):
            if slot.id == chosen_start_slot_id:
                slot_index = idx
                break
    else:
        # Если ничего не прислали, берем ID первого слота по порядку day_order
        chosen_start_slot_id = slots_queue[0].id

    # 3. Генерация сетки календаря
    calendar_preview = []
    current_date = request.start_date

    for _ in range(request.preview_length):
        weekday = current_date.weekday()
        current_slot = slots_queue[slot_index]
        day_bp = current_slot.day

        # 1. ПРЕВРАЩАЕМ ОБЪЕКТЫ В СТРОКИ
        # Замени 'm.name' на то, как у тебя реально называется колонка с названием мышцы в модели DayMuscleTarget
        # (может быть m.target_name или m.muscle_name)
        target_names = [m.muscle_group_id for m in day_bp.muscle_targets] if day_bp.muscle_targets else []

        # Обновляем проверку на отдых с использованием нового массива
        is_rest_in_split = day_bp.template_type in ["active_rest", "rest"] or len(target_names) == 0

        if weekday in request.blackout_weekdays:
            if is_rest_in_split:
                calendar_preview.append({
                    "date": current_date,
                    "is_blackout": True,
                    "is_rest_day": True,
                    "slot_id": current_slot.id,
                    "day_name": day_bp.name,
                    "muscle_targets": target_names  # <--- КЛАДЕМ МАССИВ СТРОК
                })
                slot_index = (slot_index + 1) % queue_length
            else:
                calendar_preview.append({
                    "date": current_date,
                    "is_blackout": True,
                    "is_rest_day": True,
                    "slot_id": None,
                    "day_name": "Блокировка (Отдых)",
                    "muscle_targets": []
                })
        else:
            calendar_preview.append({
                "date": current_date,
                "is_blackout": False,
                "is_rest_day": is_rest_in_split,
                "slot_id": current_slot.id,
                "day_name": day_bp.name,
                "muscle_targets": target_names  # <--- КЛАДЕМ МАССИВ СТРОК
            })
            slot_index = (slot_index + 1) % queue_length

        current_date += timedelta(days=1)

    # 4. Возвращаем объект строго по схеме SchedulePreviewResponse
    return {
        "blueprint_id": split.id,
        "start_date": request.start_date,
        "calendar": calendar_preview,
        "start_slot_id": chosen_start_slot_id
    }