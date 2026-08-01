"""Тесты склейки прогноза силы — главное: реалистичный потолок темпа.

Регрессия на баг: крутой начальный рост экстраполировался линейно на месяцы и
давал абсурд (+90 кг 1ПМ за 12 недель). Наклон должен клэмпиться потолком.
"""

import asyncio
from datetime import date, timedelta

from api.services import forecast_service

_D0 = date(2026, 1, 1)


def _weekly_series(start: float, step: float, n: int):
    # Ряд e1RM с недельным шагом дат и приростом step за точку.
    return [
        {"date": (_D0 + timedelta(days=i * 7)).isoformat(), "e1rm": start + step * i}
        for i in range(n)
    ]


def _run(level, history):
    async def fake_history(session, user_id, exercise_id):
        return {"name": "X", "history": history}

    orig = forecast_service.ExerciseSearchService.get_exercise_analytics_history
    forecast_service.ExerciseSearchService.get_exercise_analytics_history = staticmethod(fake_history)
    try:
        return asyncio.run(
            forecast_service.build_strength_forecast(None, 1, 1, level, None)
        )
    finally:
        forecast_service.ExerciseSearchService.get_exercise_analytics_history = orig


def test_steep_regression_is_capped_for_advanced():
    # Крутой рост ~8.6 кг/нед по 6 неделям, текущий ~143.
    history = _weekly_series(100.0, 8.6, 6)
    r = _run("advanced", history)
    assert r["has_data"]
    assert r["trend_basis"] == "regression"
    # Наклон обрезан реалистичным потолком (0.3%/нед).
    assert r["slope_per_week"] <= r["ceiling_slope_per_week"] + 1e-6
    assert r["ceiling_slope_per_week"] < 1.0  # ~0.43 для e1rm 143


def test_advanced_12wk_projection_is_realistic():
    history = _weekly_series(100.0, 8.6, 6)
    r = _run("advanced", history)
    current = r["current_e1rm"]
    p12 = next(p for p in r["projections"] if p["weeks"] == 12.0)
    # За 12 недель продвинутый растёт скромно — не больше ~5% (было +50%).
    assert p12["value"] <= current * 1.06


def test_beginner_cap_higher_than_advanced():
    history = _weekly_series(50.0, 5.0, 6)
    beg = _run("beginner", history)
    adv = _run("advanced", history)
    assert beg["ceiling_slope_per_week"] > adv["ceiling_slope_per_week"]


def test_slow_real_trend_not_inflated():
    # Реальный медленный рост ниже потолка — используем его, не потолок.
    history = _weekly_series(100.0, 0.2, 6)  # ~0.2 кг/нед
    r = _run("beginner", history)
    assert r["slope_per_week"] <= r["ceiling_slope_per_week"]
    assert abs(r["slope_per_week"] - 0.2) < 0.05  # реальный наклон сохранён


def test_declining_trend_no_projection():
    history = _weekly_series(120.0, -2.0, 6)  # падение
    r = _run("intermediate", history)
    assert r["has_data"]
    assert r["projections"] == []  # линию роста не строим
