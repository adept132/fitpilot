"""Тесты прогноза тренда и «что-если» (api/services/forecast.py)."""

from datetime import date, timedelta

import pytest

from api.services.forecast import (
    DIR_DOWN,
    DIR_UP,
    REALISM_ACHIEVED,
    REALISM_AMBITIOUS,
    REALISM_ON_TRACK,
    REALISM_UNREALISTIC,
    REALISM_WRONG_WAY,
    add_weeks,
    assess_realism,
    linear_trend,
    project,
    required_slope_per_week,
    weeks_to_target,
)

D0 = date(2026, 1, 1)


def series(values, step_days=7):
    """Ряд с равным шагом в днях, значения по списку."""
    return [(D0 + timedelta(days=i * step_days), v) for i, v in enumerate(values)]


# --- Регрессия ---

def test_perfect_linear_trend_recovers_slope():
    # +2 кг каждую неделю
    t = linear_trend(series([100, 102, 104, 106]))
    assert t.basis == "regression"
    assert t.n_points == 4
    assert t.slope_per_week == pytest.approx(2.0, abs=1e-6)
    assert t.stderr_per_week == pytest.approx(0.0, abs=1e-6)  # идеальная прямая


def test_daily_step_slope_is_per_week():
    # +1 в день -> 7 в неделю
    t = linear_trend(series([10, 11, 12, 13, 14], step_days=1))
    assert t.slope_per_week == pytest.approx(7.0, abs=1e-6)


def test_same_day_points_are_averaged():
    pts = [(D0, 100.0), (D0, 104.0), (D0 + timedelta(days=7), 108.0)]
    t = linear_trend(pts)
    # усреднённая первая точка = 102, вторая 108 -> +6/нед
    assert t.slope_per_week == pytest.approx(6.0, abs=1e-6)


def test_noisy_series_has_positive_stderr():
    t = linear_trend(series([100, 105, 103, 110, 108, 115]))
    assert t.slope_per_week > 0
    assert t.stderr_per_week > 0  # шум -> ненулевая ошибка


def test_two_points_no_stderr():
    t = linear_trend(series([100, 110]))
    assert t.n_points == 2
    assert t.slope_per_week == pytest.approx(10.0)
    assert t.stderr_per_week == 0.0  # для SE нужно >=3 точек


def test_single_point_basis():
    t = linear_trend([(D0, 100.0)])
    assert t.basis == "single"
    assert t.slope_per_week == 0.0
    assert t.intercept == 100.0


def test_empty_series():
    t = linear_trend([])
    assert t.basis == "insufficient"
    assert t.n_points == 0


def test_declining_trend_negative_slope():
    t = linear_trend(series([90, 89, 88, 87]))  # худеем ~1 кг/нед
    assert t.slope_per_week == pytest.approx(-1.0, abs=1e-6)


# --- Проекция ---

def test_projection_point_and_range():
    t = linear_trend(series([100, 105, 103, 110, 108, 115]))  # шумный рост
    p = project(current_value=115, trend=t, weeks=4)
    assert p.value > 115  # растём
    assert p.low < p.value < p.high  # есть диапазон из-за шума


def test_projection_no_noise_zero_range():
    t = linear_trend(series([100, 102, 104]))
    p = project(current_value=104, trend=t, weeks=3)
    assert p.value == pytest.approx(104 + 6)  # +2/нед * 3
    assert p.low == pytest.approx(p.high)  # идеальная прямая -> нет разброса


# --- weeks_to_target ---

def test_weeks_to_target_basic():
    t = linear_trend(series([100, 102, 104]))  # +2/нед
    w = weeks_to_target(current_value=104, target=120, trend=t, direction=DIR_UP)
    assert w == pytest.approx(8.0)  # (120-104)/2


def test_weeks_to_target_already_reached():
    t = linear_trend(series([100, 102]))
    assert weeks_to_target(105, 100, t, DIR_UP) == 0.0


def test_weeks_to_target_wrong_direction_returns_none():
    t = linear_trend(series([104, 102, 100]))  # падает
    # хотим вырасти до 120, а тренд падает -> недостижимо
    assert weeks_to_target(100, 120, t, DIR_UP) is None


def test_weeks_to_target_flat_returns_none():
    t = linear_trend(series([100, 100, 100]))
    assert weeks_to_target(100, 120, t, DIR_UP) is None


def test_weeks_to_target_weight_loss():
    t = linear_trend(series([90, 89, 88]))  # -1/нед
    w = weeks_to_target(current_value=88, target=82, trend=t, direction=DIR_DOWN)
    assert w == pytest.approx(6.0)


# --- required_slope / realism ---

def test_required_slope():
    assert required_slope_per_week(100, 120, 10) == pytest.approx(2.0)
    assert required_slope_per_week(100, 120, 0) is None


def test_realism_on_track():
    t = linear_trend(series([100, 102, 104]))  # +2/нед фактически
    # нужно +2/нед (20 за 10 недель) -> в темпе
    r = assess_realism(104, 124, 10, t, DIR_UP, ceiling_slope_per_week=3)
    assert r == REALISM_ON_TRACK


def test_realism_ambitious():
    t = linear_trend(series([100, 101, 102]))  # +1/нед фактически
    # нужно +2/нед, потолок 3 -> амбициозно, но возможно
    r = assess_realism(102, 122, 10, t, DIR_UP, ceiling_slope_per_week=3)
    assert r == REALISM_AMBITIOUS


def test_realism_unrealistic_above_ceiling():
    t = linear_trend(series([100, 101, 102]))
    # нужно +10/нед при потолке 3 -> нереально
    r = assess_realism(102, 152, 5, t, DIR_UP, ceiling_slope_per_week=3)
    assert r == REALISM_UNREALISTIC


def test_realism_wrong_way():
    t = linear_trend(series([104, 102, 100]))  # тренд вниз
    r = assess_realism(100, 130, 10, t, DIR_UP, ceiling_slope_per_week=3)
    assert r == REALISM_WRONG_WAY


def test_realism_achieved():
    t = linear_trend(series([100, 102]))
    assert assess_realism(125, 120, 10, t, DIR_UP) == REALISM_ACHIEVED


def test_realism_down_direction_achieved():
    t = linear_trend(series([90, 89]))
    assert assess_realism(80, 82, 10, t, DIR_DOWN) == REALISM_ACHIEVED


# --- add_weeks ---

def test_add_weeks():
    assert add_weeks(D0, 6) == D0 + timedelta(days=42)
    # round(1.5*7)=round(10.5)=10 (банкирское округление в Python)
    assert add_weeks(D0, 1.5) == D0 + timedelta(days=10)
    assert add_weeks(D0, 2) == D0 + timedelta(days=14)
