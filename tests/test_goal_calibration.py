"""Множитель исполнения: границы и условия доступности (P0-12, Задача 2)."""
from api.services.goal import params, simulate


def test_factor_is_product_of_adherence_and_success():
    factor = simulate.calibration_factor([0.8, 0.9], lift_sessions=10, success_rate=0.5)
    assert abs(factor - 0.85 * 0.5) < 1e-9


def test_factor_is_clamped_from_below():
    factor = simulate.calibration_factor([0.1, 0.1], lift_sessions=10, success_rate=0.1)
    assert factor == params.CALIBRATION_MIN_FACTOR


def test_factor_is_clamped_from_above():
    factor = simulate.calibration_factor([1.0, 1.0], lift_sessions=10, success_rate=1.0)
    assert factor == params.CALIBRATION_MAX_FACTOR


def test_too_few_windows_gives_none():
    assert simulate.calibration_factor([0.9], lift_sessions=10, success_rate=0.9) is None


def test_too_few_lift_sessions_gives_none():
    assert simulate.calibration_factor([0.9, 0.9], lift_sessions=3, success_rate=0.9) is None
