"""Загрузка входов контура «план -> факт -> решение» и запись снимков."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import UserCalendarDay, WorkoutSession

logger = logging.getLogger(__name__)


async def attach_session_to_day(
    session: AsyncSession,
    app_user_id: int,
    workout: WorkoutSession,
) -> Optional[UserCalendarDay]:
    """Закрыть плановый день завершённой сессией.

    Два правила, в порядке приоритета:
      1. явная привязка из старта (`workout.calendar_day_id`) — намерение
         пользователя, известное достоверно; переживает тренировку,
         затянувшуюся за полночь;
      2. доматчивание по дате старта — для сессий без привязки:
         спонтанных, импортированных из CSV и приехавших из офлайн-очереди.
         Без этого правила adherence был бы систематически занижен ровно у
         самых активных пользователей (офлайн — базовый сценарий, P0-02).

    День отдыха и blackout не закрываются: они не входят в знаменатель
    adherence, и «выполнить» их нечем. Уже закрытый день не перехватывается
    второй сессией — факт принадлежит первой.
    """
    day: Optional[UserCalendarDay] = None

    if workout.calendar_day_id:
        day = (await session.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.id == workout.calendar_day_id,
                UserCalendarDay.app_user_id == app_user_id,
            )
        )).scalar_one_or_none()

    if day is None:
        started_on: date = workout.started_at.date()
        day = (await session.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == app_user_id,
                UserCalendarDay.target_date == started_on,
            )
        )).scalar_one_or_none()

    if day is None or day.is_rest_day or day.is_blackout:
        return None
    if day.actual_workout_session_id is not None:
        return None

    day.status = "completed"
    day.actual_workout_session_id = workout.id
    return day


async def guarded(session: AsyncSession, label: str, work) -> Any:
    """Выполнить надстроечную работу так, чтобы её падение не уронило
    основной путь и не отравило транзакцию вызывающего.

    Голого try/except здесь недостаточно: работа идёт в ТОЙ ЖЕ сессии,
    которую вызывающий эндпоинт потом коммитит, и ошибка уровня DBAPI
    помечает транзакцию как требующую отката — следующий session.commit()
    упадёт с PendingRollbackError, хотя исключение уже поймано и
    залогировано. SAVEPOINT изолирует падение.

    Форма без `async with` — сознательная: контекст-менеджер регистрирует
    себя владельцем транзакционного контекста и ломается, если обёрнутая
    работа успела закоммитить сессию сама. Подробный разбор — в докстринге
    safe_refresh_proposals (api/services/periodization/service.py).

    `work` — уже созданная корутина; корутины ленивы, поэтому создание её
    до begin_nested() безопасно.
    """
    nested = await session.begin_nested()
    try:
        result = await work
    except Exception:  # noqa: BLE001
        logger.exception("P0-09: %s — упало, основной путь продолжается", label)
        if nested.is_active:
            await nested.rollback()
        return None
    else:
        if nested.is_active:
            await nested.commit()
        return result


def utc_today() -> date:
    """Сегодняшняя дата в UTC — единственный базис дат на сервере.

    Не `date.today()`: на Render сервер и так идёт в UTC, но на машине
    разработчика в другом поясе локальная дата расходится с UTC-датой
    сессии на несколько часов в сутки, и тесты, строящие день календаря по
    локальной дате, ловят ложное падение около полуночи. Явный базис делает
    dev и prod одинаковыми.
    """
    return datetime.now(timezone.utc).date()


async def mark_missed_days(
    session: AsyncSession, app_user_id: int, today: date
) -> int:
    """Пометить прошедшие плановые дни как пропущенные.

    Лениво, при обращении к контексту — тем же приёмом «тихо доделать при
    обращении», которым живёт ensure_horizon. Отдельного планировщика в
    проекте нет и заводить его не нужно.

    Сегодняшний день не трогаем: он ещё не кончился. Дни отдыха и blackout
    пропустить нельзя по определению — они не в знаменателе adherence.
    """
    result = await session.execute(
        update(UserCalendarDay)
        .where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date < today,
            UserCalendarDay.status == "planned",
            UserCalendarDay.is_rest_day.is_(False),
            UserCalendarDay.is_blackout.is_(False),
        )
        .values(status="missed")
    )
    return int(result.rowcount or 0)


@dataclass(frozen=True)
class Adherence:
    planned_days: int
    completed_days: int
    missed_days: int


async def adherence_for_range(
    session: AsyncSession, app_user_id: int, start: date, end: date
) -> Adherence:
    """Исполняемость на отрезке дат по рабочим дням.

    Дни отдыха и blackout в знаменатель не входят: их невыполнение не
    является пропуском.
    """
    rows = (await session.execute(
        select(UserCalendarDay.status).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date >= start,
            UserCalendarDay.target_date <= end,
            UserCalendarDay.is_rest_day.is_(False),
            UserCalendarDay.is_blackout.is_(False),
        )
    )).scalars().all()

    return Adherence(
        planned_days=len(rows),
        completed_days=sum(1 for s in rows if s == "completed"),
        missed_days=sum(1 for s in rows if s == "missed"),
    )
