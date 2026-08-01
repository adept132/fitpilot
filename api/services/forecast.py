"""Прогноз тренда и «что-если» по временному ряду метрики.

Модуль метрика-агностичный: работает с рядом (дата, значение) и годится для
силы (e1RM), веса тела, % жира, замеров, частоты. Никакой БД — чистые функции,
целиком покрываемые тестами. Конвертация e1RM↔(вес,повторы) и fallback на
коэффициент автопрогрессии живут в вызывающем коде (эндпоинте), т.к. они
метрика-специфичны.

Метод: линейная регрессия (OLS) по точкам (день, значение). Наклон — единиц в
неделю; стандартная ошибка наклона даёт доверительный диапазон прогноза.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Tuple

# Направление «улучшения» цели: для силы/веса-массы больше = лучше (up),
# для снижения веса/жира меньше = лучше (down).
DIR_UP = "up"
DIR_DOWN = "down"

# Ярлыки реалистичности требуемого темпа.
REALISM_ON_TRACK = "on_track"      # укладываемся в текущем темпе
REALISM_AMBITIOUS = "ambitious"    # нужно быстрее, но в пределах разумного
REALISM_UNREALISTIC = "unrealistic"  # быстрее биологического потолка
REALISM_WRONG_WAY = "wrong_way"    # тренд идёт в другую сторону
REALISM_ACHIEVED = "achieved"      # цель уже достигнута


@dataclass
class TrendResult:
    slope_per_week: float          # изменение значения за неделю
    intercept: float               # значение на день первой точки
    stderr_per_week: float         # стандартная ошибка наклона (в неделю)
    n_points: int
    span_days: int                 # разброс дат (последняя − первая)
    basis: str                     # "regression" | "single" | "insufficient"


@dataclass
class Projection:
    weeks: float
    value: float                   # точечный прогноз
    low: float                     # нижняя граница диапазона
    high: float                    # верхняя граница диапазона


def linear_trend(points: List[Tuple[date, float]]) -> TrendResult:
    """OLS по (день от первой точки, значение).

    Точки с одинаковой датой усредняются (в один день может быть 2 тренировки).
    basis: "regression" при ≥2 различных дней, иначе "single"/"insufficient".
    """
    # Усредняем значения по одинаковым датам, сортируем по дате.
    by_date: dict[date, List[float]] = {}
    for d, v in points:
        by_date.setdefault(d, []).append(float(v))
    agg = sorted((d, sum(vs) / len(vs)) for d, vs in by_date.items())

    n = len(agg)
    if n == 0:
        return TrendResult(0.0, 0.0, 0.0, 0, 0, "insufficient")
    if n == 1:
        return TrendResult(0.0, agg[0][1], 0.0, 1, 0, "single")

    first_day = agg[0][0]
    xs = [(d - first_day).days for d, _ in agg]
    ys = [v for _, v in agg]
    span_days = xs[-1]

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))

    if sxx == 0:  # все точки в один день после агрегации — сюда не попадём, но защита
        return TrendResult(0.0, mean_y, 0.0, n, 0, "single")

    slope_day = sxy / sxx
    intercept = mean_y - slope_day * mean_x

    # Стандартная ошибка наклона (в день), затем в неделю.
    stderr_day = 0.0
    if n >= 3:
        sse = sum((y - (intercept + slope_day * x)) ** 2 for x, y in zip(xs, ys))
        mse = sse / (n - 2)
        stderr_day = math.sqrt(mse / sxx) if mse > 0 else 0.0

    return TrendResult(
        slope_per_week=slope_day * 7,
        intercept=intercept,
        stderr_per_week=stderr_day * 7,
        n_points=n,
        span_days=span_days,
        basis="regression",
    )


def project(
    current_value: float,
    trend: TrendResult,
    weeks: float,
    z: float = 1.0,
) -> Projection:
    """Прогноз значения через `weeks` недель от текущего.

    Неопределённость растёт линейно со сроком: диапазон = point ± z·SE·weeks.
    z=1 — умеренный диапазон (~68%); вызывающий код может расширить.
    """
    point = current_value + trend.slope_per_week * weeks
    margin = abs(trend.stderr_per_week * weeks * z)
    return Projection(weeks=weeks, value=point, low=point - margin, high=point + margin)


def weeks_to_target(
    current_value: float,
    target: float,
    trend: TrendResult,
    direction: str = DIR_UP,
) -> Optional[float]:
    """Сколько недель до достижения target в текущем темпе.

    None, если цель недостижима текущим трендом (наклон ~0 или в другую сторону).
    Если цель уже достигнута — 0.
    """
    if _reached(current_value, target, direction):
        return 0.0

    slope = trend.slope_per_week
    needed = target - current_value  # знак = нужное направление изменения

    # Тренд должен двигать значение в сторону цели.
    if abs(slope) < 1e-9:
        return None
    if (needed > 0) != (slope > 0):
        return None  # тренд идёт не туда

    weeks = needed / slope
    return weeks if weeks >= 0 else None


def required_slope_per_week(
    current_value: float, target: float, deadline_weeks: float
) -> Optional[float]:
    """Какой темп нужен, чтобы достичь target к сроку. None при некорректном сроке."""
    if deadline_weeks <= 0:
        return None
    return (target - current_value) / deadline_weeks


def assess_realism(
    current_value: float,
    target: float,
    deadline_weeks: Optional[float],
    trend: TrendResult,
    direction: str = DIR_UP,
    ceiling_slope_per_week: Optional[float] = None,
) -> str:
    """Оценка реалистичности цели к сроку относительно фактического темпа.

    ceiling_slope_per_week — «биологический потолок» (напр. из коэффициента
    автопрогрессии для силы); превышение помечаем как unrealistic.
    """
    if _reached(current_value, target, direction):
        return REALISM_ACHIEVED

    needed = target - current_value
    # Цель требует движения в сторону, противоположную «улучшению».
    if direction == DIR_UP and needed < 0:
        return REALISM_ACHIEVED
    if direction == DIR_DOWN and needed > 0:
        return REALISM_ACHIEVED

    if deadline_weeks is None or deadline_weeks <= 0:
        # Без срока: судим по знаку тренда.
        if abs(trend.slope_per_week) < 1e-9:
            return REALISM_AMBITIOUS
        return REALISM_ON_TRACK if (needed > 0) == (trend.slope_per_week > 0) else REALISM_WRONG_WAY

    req = required_slope_per_week(current_value, target, deadline_weeks)
    if req is None:
        return REALISM_AMBITIOUS

    actual = trend.slope_per_week
    # Тренд в другую сторону от требуемого.
    if abs(actual) >= 1e-9 and (req > 0) != (actual > 0):
        return REALISM_WRONG_WAY

    req_mag = abs(req)
    actual_mag = abs(actual)

    if ceiling_slope_per_week is not None and req_mag > abs(ceiling_slope_per_week) * 1.05:
        return REALISM_UNREALISTIC
    if actual_mag >= req_mag:
        return REALISM_ON_TRACK
    return REALISM_AMBITIOUS


def _reached(current_value: float, target: float, direction: str) -> bool:
    if direction == DIR_DOWN:
        return current_value <= target
    return current_value >= target


def add_weeks(start: date, weeks: float) -> date:
    """Дата через `weeks` недель (округляя до дней)."""
    return start + timedelta(days=round(weeks * 7))
