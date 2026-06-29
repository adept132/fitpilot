from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.app_user_service import get_current_app_user
from api.services.models import UserCalendarDay, WorkoutPlan
from api.services.scheduling_engine import SchedulingEngine
from app.database import get_session

router = APIRouter(prefix="/calendar", tags=["Calendar Workspace"])


@router.get("/day/{local_date}")
async def get_day_context(
        local_date: date,
        session: AsyncSession = Depends(get_session),
        current_user = Depends(get_current_app_user)
):
    # 1. ТИХО ПРОВЕРЯЕМ И ДОСТРАИВАЕМ ГОРИЗОНТ
    await SchedulingEngine.ensure_horizon(session, current_user.id, local_date)

    # 2. Ищем контекст дня
    stmt = (
        select(UserCalendarDay)
        .where(
            UserCalendarDay.app_user_id == current_user.id,
            UserCalendarDay.target_date == local_date
        )
        .options(selectinload(UserCalendarDay.plan))
    )
    result = await session.execute(stmt)
    cal_day = result.scalar_one_or_none()

    if not cal_day:
        return {"status": "empty"}

    # 3. Идеальный контракт данных для фронтенда
    response = {
        "calendar_day_id": cal_day.id,
        "status": cal_day.status,
        "is_rest_day": cal_day.is_rest_day,
        "is_blackout": cal_day.is_blackout,
        "date": cal_day.target_date.strftime("%Y-%m-%d"),
        "meta": {  # <--- Теперь фронтенд найдет свои теги!
            "meso_tag": cal_day.meso_tag,
            "micro_tag": cal_day.micro_tag,
            "phase_number": cal_day.mesocycle_phase_number,
            "day_name": cal_day.day_tag
        },
        "plan": None # По умолчанию Свободная тренировка (без плана)
    }

    # Если план реально привязан, отдаем его данные
    if cal_day.plan and not cal_day.is_rest_day and not cal_day.is_blackout:
        response["plan"] = {
            "id": cal_day.plan.id,
            "name": cal_day.plan.name
        }

    return response

@router.get("/range")
async def get_calendar_range(
        start_date: date,
        end_date: date,
        session: AsyncSession = Depends(get_session),
        current_user = Depends(get_current_app_user)
):
    """Выгружает кусок реального календаря для отрисовки сетки на фронтенде."""
    stmt = (
        select(UserCalendarDay)
        .where(
            UserCalendarDay.app_user_id == current_user.id,
            UserCalendarDay.target_date >= start_date,
            UserCalendarDay.target_date <= end_date
        )
        .order_by(UserCalendarDay.target_date)
    )
    result = await session.execute(stmt)
    days = result.scalars().all()

    # Форматируем ответ так, чтобы фронтенду (твоей ScheduleModal)
    # было удобно положить это в свой scheduleMap
    calendar_map = []
    for d in days:
        calendar_map.append({
            "date": d.target_date.strftime("%Y-%m-%d"),
            "is_blackout": d.is_blackout,
            "is_rest_day": d.is_rest_day,
            "status": d.status,
            "day_name": d.day_tag if not d.is_rest_day else "Отдых",
            "plan_id": d.plan_id
        })

    return {"calendar": calendar_map}

@router.get("/day-by-id/{day_id}")
async def get_day_by_id(
        day_id: int,
        session: AsyncSession = Depends(get_session),
        current_user = Depends(get_current_app_user)
):
    stmt = (
        select(UserCalendarDay)
        .where(
            UserCalendarDay.id == day_id,
            UserCalendarDay.app_user_id == current_user.id
        )
        .options(selectinload(UserCalendarDay.plan))
    )
    result = await session.execute(stmt)
    cal_day = result.scalar_one_or_none()

    if not cal_day:
        return {"status": "empty"}

    return {
        "calendar_day_id": cal_day.id,
        "status": cal_day.status,
        "is_rest_day": cal_day.is_rest_day,
        "is_blackout": cal_day.is_blackout,
        "date": cal_day.target_date.strftime("%Y-%m-%d"),
        "meta": {
            "meso_tag": cal_day.meso_tag,
            "micro_tag": cal_day.micro_tag,
            "phase_number": cal_day.mesocycle_phase_number,
            "day_name": cal_day.day_tag
        },
        "plan": {"id": cal_day.plan.id, "name": cal_day.plan.name} if cal_day.plan else None
    }