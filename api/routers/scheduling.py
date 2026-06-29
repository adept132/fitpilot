from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional
from datetime import date, timedelta
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

# Импортируй свои схемы и сервисы
from api.schemas.splits import CalendarDayPreview, SchedulePreviewResponse, SchedulePreviewRequest
from api.services.app_user_service import get_current_app_user
from api.services.models import AppUserMesocycle, Mesocycle, AppUserMicrocycle, WorkoutPlan
from api.services.scheduling_engine import SchedulingEngine  # <-- НЕ ЗАБУДЬ ИМПОРТ
from app.database import get_session

router = APIRouter(prefix="/scheduling", tags=["Scheduling"])


@router.post("/preview", response_model=SchedulePreviewResponse)
async def generate_schedule_preview(
        request: SchedulePreviewRequest,
        session: AsyncSession = Depends(get_session),
        current_user=Depends(get_current_app_user)
):
    # 1. Достаем микроцикл (настраиваемый сплит).
    # Предполагаем, что blueprint_id с фронта теперь указывает на AppUserMicrocycle.id
    micro_stmt = (
        select(AppUserMicrocycle)
        .where(
            AppUserMicrocycle.id == request.blueprint_id,
            AppUserMicrocycle.app_user_id == current_user.id
        )
    )
    micro_res = await session.execute(micro_stmt)
    user_micro = micro_res.scalar_one_or_none()

    if not user_micro:
        raise HTTPException(status_code=404, detail="Сплит (микроцикл) не найден")

    days_mapping = user_micro.days_mapping
    split_length = user_micro.length_days

    # 2. Достаем активный мезоцикл и его фазы
    meso_stmt = select(AppUserMesocycle).where(
        AppUserMesocycle.app_user_id == current_user.id,
        AppUserMesocycle.is_active == True
    )
    meso_res = await session.execute(meso_stmt)
    user_meso = meso_res.scalar_one_or_none()

    phases_list = []
    days_per_phase = 7  # Дефолт
    if user_meso:
        strategy_stmt = (
            select(Mesocycle)
            .where(Mesocycle.id == user_meso.mesocycle_id)
            .options(selectinload(Mesocycle.phases))
        )
        strategy_res = await session.execute(strategy_stmt)
        strategy = strategy_res.scalar_one()
        phases_list = sorted(strategy.phases, key=lambda p: p.phase_number)
        days_per_phase = user_meso.microcycle_length

    # 3. Выкачиваем все планы пользователя для автоподбора
    plans_stmt = select(WorkoutPlan).where(WorkoutPlan.app_user_id == current_user.id)
    plans_res = await session.execute(plans_stmt)
    user_plans = list(plans_res.scalars().all())

    # 4. Генерация календаря (Скользящая очередь с учетом координат)
    calendar_preview = []
    current_date = request.start_date
    split_day_counter = 1
    total_workout_days_passed = 0

    for _ in range(request.preview_length):
        weekday = current_date.weekday()  # 0 = ПН, 6 = ВС

        # Вычисляем фазу мезоцикла
        meso_tag_calc = "medium"  # Фолбэк, если мезоцикл не задан
        if phases_list:
            current_phase_idx = total_workout_days_passed // days_per_phase
            if current_phase_idx >= len(phases_list):
                current_phase_idx = len(phases_list) - 1
            meso_tag_calc = phases_list[current_phase_idx].effort_tier

        # Берем настройки дня из сплита
        day_config = days_mapping.get(str(split_day_counter), {"type": "rest", "tag": None})
        day_tag_calc = day_config.get("tag")
        micro_tag_calc = day_config.get("type")

        is_rest_in_split = micro_tag_calc == "rest" or day_tag_calc is None
        is_banned = weekday in request.blackout_weekdays

        # Формируем читаемое название дня (будет показано на фронте)
        day_name_display = "Отдых"
        if not is_rest_in_split:
            # Черновой вариант названия (например "Push (hard)")
            day_name_display = f"{str(day_tag_calc).capitalize()} ({micro_tag_calc})"

        # СЦЕНАРИЙ 1: Забаненный день
        if is_banned:
            if is_rest_in_split:
                calendar_preview.append(CalendarDayPreview(
                    date=current_date,
                    is_blackout=True,
                    is_rest_day=True,
                    slot_id=None,
                    day_name=day_name_display,
                    muscle_targets=[]
                ))
                split_day_counter = (split_day_counter % split_length) + 1
                total_workout_days_passed += 1
            else:
                calendar_preview.append(CalendarDayPreview(
                    date=current_date,
                    is_blackout=True,
                    is_rest_day=True,
                    slot_id=None,
                    day_name="Блокировка (Отдых)",
                    muscle_targets=[]
                ))

        # СЦЕНАРИЙ 2: Обычный день (Тренировка или запланированный отдых)
        else:
            is_rest_day = is_rest_in_split
            plan_id_to_save = None

            if not is_rest_day:
                # Магия автоподбора: ищем подходящий план
                plan_id_to_save = SchedulingEngine._score_and_find_best_plan(
                    plans=user_plans,
                    day_tag=day_tag_calc,
                    micro_tag=micro_tag_calc,
                    meso_tag=meso_tag_calc
                )

                # Если план найден, показываем его реальное название в календаре!
                if plan_id_to_save:
                    matched_plan = next((p for p in user_plans if p.id == plan_id_to_save), None)
                    if matched_plan:
                        day_name_display = matched_plan.name
                else:
                    day_name_display += " ⚠️ (План не найден)"

            calendar_preview.append(CalendarDayPreview(
                date=current_date,
                is_blackout=False,
                is_rest_day=is_rest_day,
                slot_id=str(plan_id_to_save) if plan_id_to_save else None,  # Отдаем ID плана
                day_name=day_name_display,
                muscle_targets=[]  # Можно пробросить теги, если нужно для фронта
            ))

            split_day_counter = (split_day_counter % split_length) + 1
            total_workout_days_passed += 1

        current_date += timedelta(days=1)

    return SchedulePreviewResponse(
        blueprint_id=request.blueprint_id,
        start_date=request.start_date,
        calendar=calendar_preview
    )