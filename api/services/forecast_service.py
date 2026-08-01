"""Прогноз силы: склейка ряда e1RM с чистым forecast + специфика автопрогрессии.

Тонкий слой над forecast.py: достаёт историю e1RM упражнения, строит тренд,
считает «потолок» темпа из коэффициента автопрогрессии и готовит точки для
прогнозной линии на графике. Интерактивный «что-если» клиент считает локально
из slope/current/ceiling — так слайдер отзывчив без сети.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from api.services.exercise_search_service import ExerciseSearchService
from api.services.forecast import add_weeks, linear_trend

# Горизонты прогнозной линии (недели от последней тренировки).
FORECAST_HORIZONS_WEEKS = [2.0, 4.0, 6.0, 8.0, 12.0]

# Реалистичный потолок недельного прироста e1RM (доля от текущего), по уровню.
# Натуральные темпы роста силы скромны и быстро замедляются, особенно у
# опытных. Без этого потолка линейная экстраполяция даёт абсурд (+90 кг 1ПМ
# за 12 недель). Наклоном ограничиваем ВСЕ прогнозы — и regression, и factor.
WEEKLY_GROWTH_CAP_PCT = {
    "beginner": 0.010,      # ~1%/нед — быстрый прогресс новичка
    "intermediate": 0.005,  # ~0.5%/нед
    "advanced": 0.003,      # ~0.3%/нед — опытные растут медленно
}
_DEFAULT_CAP_PCT = 0.010

# Ширина коридора прогноза как доля прогнозируемого прироста (±). Держит
# диапазон соразмерным сигналу и не даёт нижней границе уйти ниже текущего 1ПМ.
FORECAST_BAND_FRACTION = 0.5

# Разумные границы оценки частоты тренировок упражнения (сессий в неделю).
_MIN_SPW = 0.25
_MAX_SPW = 7.0
_DEFAULT_SPW = 1.5


def _sessions_per_week(n_points: int, span_days: int) -> float:
    if n_points < 2 or span_days <= 0:
        return _DEFAULT_SPW
    avg_gap = span_days / (n_points - 1)
    spw = 7.0 / avg_gap
    return max(_MIN_SPW, min(_MAX_SPW, spw))


async def build_strength_forecast(
    session: AsyncSession,
    user_id: int,
    exercise_id: int,
    experience_level: Optional[str],
    settings: Optional[dict],
) -> Optional[dict]:
    """Прогноз e1RM по упражнению. None — если упражнение недоступно юзеру."""
    data = await ExerciseSearchService.get_exercise_analytics_history(
        session=session, user_id=user_id, exercise_id=exercise_id
    )
    if data is None:
        return None

    name = data["name"]
    history = data.get("history", [])

    base = {
        "exercise_id": exercise_id,
        "name": name,
        "has_data": False,
        "current_e1rm": None,
        "last_date": None,
        "slope_per_week": None,
        "trend_basis": "insufficient",
        "n_points": 0,
        "ceiling_slope_per_week": None,
        "sessions_per_week": None,
        "projections": [],
    }
    if not history:
        return base

    points = [(date.fromisoformat(p["date"]), float(p["e1rm"])) for p in history]
    trend = linear_trend(points)

    last_date, current_e1rm = points[-1]
    spw = _sessions_per_week(trend.n_points, trend.span_days)  # для инфо в ответе

    # Реалистичный потолок недельного прироста для этого уровня.
    level = (experience_level or "beginner").strip().lower()
    cap_pct = WEEKLY_GROWTH_CAP_PCT.get(level, _DEFAULT_CAP_PCT)
    ceiling_slope = current_e1rm * cap_pct

    # Наклон прогноза:
    #  - regression: реальный темп из истории, но НЕ выше реалистичного потолка
    #    (крутой начальный рост нельзя экстраполировать линейно на месяцы);
    #  - недостаточно истории: берём сам потолок как консервативную оценку.
    basis = trend.basis
    if basis == "regression":
        effective_slope = min(trend.slope_per_week, ceiling_slope)
    else:
        effective_slope = ceiling_slope
        basis = "factor"

    # Честность: линейно экстраполировать силу в будущее осмысленно только при
    # устойчивом РОСТЕ. Падающий/нулевой тренд (детренинг, плато, шумные данные)
    # линией роста не рисуем — иначе прогноз уходит в абсурд (вплоть до минуса).
    # Клиент по slope_per_week <= 0 покажет «нет устойчивого роста / плато».
    projections: List[dict] = []
    if effective_slope > 0:
        # Верхний предел прогноза — утроение текущего e1RM: защита от абсурдного
        # роста на коротких/шумных положительных трендах.
        cap = current_e1rm * 3.0
        for weeks in FORECAST_HORIZONS_WEEKS:
            # Точку считаем по КЛЭМПНУТОМУ наклону.
            gain = effective_slope * weeks
            value = current_e1rm + gain
            # Коридор соразмерен прогнозируемому приросту (вилка темпа ±band),
            # а НЕ статистической ошибке шумной регрессии: та раздувала диапазон
            # и опускала нижнюю границу ниже текущего 1ПМ («прогноз снижения»),
            # что для прогноза роста бессмысленно. Нижняя граница = более
            # медленный рост, но не откат (>= текущего).
            band = gain * FORECAST_BAND_FRACTION
            projections.append(
                {
                    "date": add_weeks(last_date, weeks).isoformat(),
                    "weeks": weeks,
                    "value": round(min(cap, value), 1),
                    "low": round(value - band, 1),   # = current + gain*(1-band) >= current
                    "high": round(min(cap, value + band), 1),
                }
            )

    return {
        "exercise_id": exercise_id,
        "name": name,
        "has_data": True,
        "current_e1rm": round(current_e1rm, 1),
        "last_date": last_date.isoformat(),
        "slope_per_week": round(effective_slope, 3),
        "trend_basis": basis,
        "n_points": trend.n_points,
        "ceiling_slope_per_week": round(ceiling_slope, 3),
        "sessions_per_week": round(spw, 2),
        "projections": projections,
    }
