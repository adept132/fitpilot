"""Тесты композиции тела: US Navy % жира и ИМТ (чистые функции)."""

import pytest

from api.services.body_service import compute_bmi, navy_body_fat


def test_navy_male_typical():
    bf = navy_body_fat("male", 180, 88, 38)
    assert 15 <= bf <= 22  # средний мужчина


def test_navy_male_lean_lower_than_average():
    lean = navy_body_fat("male", 180, 80, 38)
    avg = navy_body_fat("male", 180, 88, 38)
    assert lean < avg
    assert 8 <= lean <= 16


def test_navy_female_needs_hips():
    assert navy_body_fat("female", 165, 70, 32) is None
    bf = navy_body_fat("female", 165, 70, 32, 95)
    assert 18 <= bf <= 32  # средняя женщина обычно выше мужчины


@pytest.mark.parametrize("g", ["ж", "female", "Женский", "woman"])
def test_navy_recognizes_female_variants(g):
    assert navy_body_fat(g, 165, 70, 32, 95) is not None


def test_navy_missing_data_returns_none():
    assert navy_body_fat("male", None, 88, 38) is None
    assert navy_body_fat("male", 180, None, 38) is None
    assert navy_body_fat("male", 180, 88, None) is None


def test_navy_invalid_geometry_returns_none():
    # талия <= шея -> log10 неопределён
    assert navy_body_fat("male", 180, 38, 40) is None


def test_navy_clamped_range():
    # экстремальные значения не выходят за [3, 60]
    bf = navy_body_fat("male", 150, 150, 30)
    assert bf is None or 3.0 <= bf <= 60.0


@pytest.mark.parametrize(
    "w,h,expected",
    [(84, 180, 25.9), (60, 165, 22.0), (0, 180, None), (80, 0, None), (None, 180, None)],
)
def test_bmi(w, h, expected):
    result = compute_bmi(w, h)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, abs=0.1)
