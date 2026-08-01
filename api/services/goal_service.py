"""Статус цели: текущее значение, прогресс, ETA, реалистичность.

Единый расчёт для всех типов целей поверх forecast.py:
- strength   — вес × повторы по упражнению (метрика — e1RM);
- bodyweight — вес тела;
- body_fat   — % жира;
- measurement— обхват (metric_key);
- frequency  — тренировок в неделю.

Прогресс считается на лету из истории соответствующей метрики (единый источник
с графиками), поэтому всегда актуален. Направление (рост/снижение) выводится из
текущего значения и цели.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.body_service import get_metric_series
from api.services.exercise_search_service import ExerciseSearchService
from api.services.forecast import (
    DIR_DOWN,
    DIR_UP,
    add_weeks,
    assess_realism,
    linear_trend,
    weeks_to_target,
)
from api.services.forecast_service import WEEKLY_GROWTH_CAP_PCT, _DEFAULT_CAP_PCT
from api.services.models import WorkoutSession

GOAL_STRENGTH = "strength"
GOAL_BODYWEIGHT = "bodyweight"
GOAL_BODY_FAT = "body_fat"
GOAL_MEASUREMENT = "measurement"
GOAL_FREQUENCY = "frequency"

_EPS = 1e-9


def epley_e1rm(weight: float, reps: int) -> float:
    """e1RM по Эпли без RIR — консистентно с рядом истории и графиком."""
    return weight * (1.0 + reps / 30.0) if reps > 0 else weight


def _empty_status(target_display: Optional[float]) -> dict:
    return {
        "current_value": None,
        "current_e1rm": None,
        "target_e1rm": None,
        "target_display": target_display,
        "progress_percentage": 0.0,
        "eta_date": None,
        "realism": "insufficient",
        "direction": DIR_UP,
        "has_data": False,
    }


async def _strength_series(
    session: AsyncSession, user_id: int, exercise_id: int
) -> List[Tuple[date, float]]:
    data = await ExerciseSearchService.get_exercise_analytics_history(
        session=session, user_id=user_id, exercise_id=exercise_id
    )
    if not data:
        return []
    return [(date.fromisoformat(p["date"]), float(p["e1rm"])) for p in data["history"]]


async def _frequency_series(
    session: AsyncSession, user_id: int
) -> List[Tuple[date, float]]:
    """Недельная частота: (понедельник недели, число завершённых тренировок).

    Заполняем и «нулевые» недели между первой и последней тренировкой — пропуск
    это частота 0, иначе тренд был бы завышен."""
    rows = list((await session.execute(
        select(func.date_trunc("week", WorkoutSession.started_at))
        .where(
            WorkoutSession.app_user_id == user_id,
            WorkoutSession.status == "finished",
        )
    )).scalars().all())
    if not rows:
        return []
    weeks = [r.date() if hasattr(r, "date") else r for r in rows]
    counts: dict = {}
    for w in weeks:
        counts[w] = counts.get(w, 0) + 1

    start, end = min(counts), max(counts)
    series: List[Tuple[date, float]] = []
    cur = start
    while cur <= end:
        series.append((cur, float(counts.get(cur, 0))))
        cur += timedelta(days=7)
    return series


def _baseline(series: List[Tuple[date, float]]) -> float:
    """Стартовое значение — первая точка истории. Прогресс отражает весь путь
    к цели (нагляднее, чем «с момента создания цели», когда прогресс был бы 0)."""
    return series[0][1] if series else 0.0


def _progress(baseline: float, current: float, target: float) -> float:
    span = target - baseline
    if abs(span) < _EPS:
        return 100.0
    p = (current - baseline) / span * 100.0
    return round(max(0.0, min(100.0, p)), 1)


class _Trend:
    """Обёртка с нужным forecast-функциям полем slope_per_week."""

    def __init__(self, slope: float):
        self.slope_per_week = slope


async def compute_goal_status(
    session: AsyncSession,
    goal,
    experience_level: Optional[str],
    settings: Optional[dict],
) -> dict:
    """Единый статус цели любого типа."""
    gt = goal.goal_type

    # 1. Ряд метрики + target в единицах метрики + отображаемые значения.
    if gt == GOAL_STRENGTH:
        if goal.exercise_id is None:
            return _empty_status(goal.target_value)
        series = await _strength_series(session, goal.app_user_id, goal.exercise_id)
        reps = goal.target_reps or 1
        target = epley_e1rm(float(goal.target_value), reps)
        target_display = float(goal.target_value)
    elif gt in (GOAL_BODYWEIGHT, GOAL_BODY_FAT):
        metric = "weight" if gt == GOAL_BODYWEIGHT else "body_fat"
        series = await get_metric_series(session, goal.app_user_id, metric)
        target = float(goal.target_value)
        target_display = target
    elif gt == GOAL_MEASUREMENT:
        series = await get_metric_series(session, goal.app_user_id, goal.metric_key or "")
        target = float(goal.target_value)
        target_display = target
    elif gt == GOAL_FREQUENCY:
        series = await _frequency_series(session, goal.app_user_id)
        target = float(goal.target_value)
        target_display = target
    else:
        return _empty_status(goal.target_value)

    status = _empty_status(target_display)
    if not series:
        return status

    trend = linear_trend(series)
    last_date, current = series[-1]

    # Частоту сглаживаем: current = среднее последних 4 недель (недельные данные шумны).
    if gt == GOAL_FREQUENCY:
        tail = [v for _, v in series[-4:]]
        current = sum(tail) / len(tail)

    baseline = _baseline(series)

    # 2. Направление цели — по СТАРТОВОМУ значению относительно цели (не по
    # текущему): иначе перевыполненная цель похудения (79 при цели 80) читалась
    # бы как «набор». strength — всегда вверх.
    if gt == GOAL_STRENGTH:
        direction = DIR_UP
    elif abs(target - baseline) < _EPS:
        # цель уже на старте — ориентируемся на тренд
        direction = DIR_UP if trend.slope_per_week >= 0 else DIR_DOWN
    else:
        direction = DIR_DOWN if target < baseline else DIR_UP

    # 3. Потолок реалистичности: сила — по уровню; вес — ~1% массы/нед; иначе нет.
    ceiling = None
    if gt == GOAL_STRENGTH:
        cap_pct = WEEKLY_GROWTH_CAP_PCT.get(
            (experience_level or "beginner").strip().lower(), _DEFAULT_CAP_PCT
        )
        ceiling = current * cap_pct
    elif gt == GOAL_BODYWEIGHT:
        ceiling = current * 0.01  # безопасный темп изменения веса ~1%/нед

    progress = _progress(baseline, current, target)

    deadline_weeks = None
    if goal.deadline is not None:
        deadline_weeks = (goal.deadline - last_date).days / 7.0

    weeks = weeks_to_target(current, target, trend, direction)
    eta_date = None
    if weeks is not None and weeks > 0:
        eta_date = add_weeks(last_date, weeks).isoformat()
    elif weeks == 0:
        eta_date = last_date.isoformat()

    achieved = (direction == DIR_DOWN and current <= target) or (
        direction == DIR_UP and current >= target
    )
    if achieved:
        realism = assess_realism(current, target, deadline_weeks, trend, direction, ceiling)
    elif gt != GOAL_STRENGTH and trend.n_points < 2:
        # Есть данные и прогресс, но записи только за один день — тренд во
        # времени не построить. Честно: копим данные (без прогноза темпа/ETA).
        realism = "insufficient"
        eta_date = None
    else:
        realism = assess_realism(current, target, deadline_weeks, trend, direction, ceiling)

    # 4. Отображаемое «текущее».
    if gt == GOAL_STRENGTH:
        reps = goal.target_reps or 1
        current_display = current / (1.0 + reps / 30.0) if reps > 0 else current
        status["current_e1rm"] = round(current, 1)
        status["target_e1rm"] = round(target, 1)
    else:
        current_display = current

    status.update({
        "current_value": round(current_display, 1),
        "progress_percentage": progress,
        "eta_date": eta_date,
        "realism": realism,
        "direction": direction,
        "has_data": True,
    })
    return status
