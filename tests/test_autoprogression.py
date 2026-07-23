"""Характеризационные тесты ядра автопрогрессии (api/services/autoprogression.py).

Фиксируют текущее поведение перед тем, как на эти функции ляжет усталостная
модель. Реализацию не меняем: падение теста — повод разобраться, а не подогнать.
"""

import pytest

from api.services.autoprogression import (
    DEFAULT_RIR,
    effort_to_rir,
    progression_factor_for,
    round_weight_for_equipment,
    set_target_value,
    weight_for_target,
)


@pytest.mark.parametrize(
    "level,expected",
    [
        ("warmup", 4),
        ("warmup_effort", 4),
        ("light", 3),
        ("easy", 3),
        ("medium", 2),
        ("prefailure", 1),
        ("failure", 0),
    ],
)
def test_effort_to_rir_known_levels(level, expected):
    assert effort_to_rir(level) == expected


def test_effort_to_rir_is_case_and_space_insensitive():
    assert effort_to_rir("  FAILURE ") == 0


@pytest.mark.parametrize("level", [None, "", "unknown"])
def test_effort_to_rir_falls_back_to_default(level):
    assert effort_to_rir(level) == DEFAULT_RIR


def test_set_target_value_folds_rir_into_epley():
    # 100 * (1 + (10 + 2) / 30) = 140.0
    assert set_target_value(100.0, 10, 2) == pytest.approx(140.0)


def test_weight_for_target_is_inverse_of_set_target_value():
    for weight, reps, rir in [(100.0, 10, 2), (60.0, 5, 0), (42.5, 12, 3)]:
        target = set_target_value(weight, reps, rir)
        assert weight_for_target(target, reps, rir) == pytest.approx(weight)


def test_round_weight_snaps_barbell_to_plate_step():
    assert round_weight_for_equipment(101.0, ["barbell"], "kg") == pytest.approx(100.0)


def test_round_weight_never_returns_below_one_step():
    # Округление вниз дало бы 0 — возвращаем минимум один шаг.
    assert round_weight_for_equipment(1.0, ["barbell"], "kg") == pytest.approx(2.5)


def test_round_weight_block_machine_is_always_in_pounds():
    # Блочный тренажёр считается в фунтах с шагом 10 и возвращается в кг.
    assert round_weight_for_equipment(100.0, ["block_machine"], "kg") == pytest.approx(
        99.79, abs=0.01
    )


def test_round_weight_barbell_wins_over_dumbbell():
    # При нескольких видах оборудования приоритет у штанги (_STEP_PRIORITY).
    # Дефолтные plate_kg и dumbbell_kg совпадают (2.5), поэтому порядок приоритета
    # ничего не пинит — передаём steps с РАЗНЫМИ шагами, чтобы исходы различались.
    #
    # Штанга (побеждает по _STEP_PRIORITY): шаг plate_kg = 4.
    #   round(101.0 / 4) * 4 = round(25.25) * 4 = 25 * 4 = 100.0 кг
    #
    # Если бы победили гантели: шаг dumbbell_kg = 6.
    #   round(101.0 / 6) * 6 = round(16.8333...) * 6 = 17 * 6 = 102.0 кг ≠ 100.0
    #   (проверено: round_weight_for_equipment(101.0, ["dumbbell"], "kg",
    #    steps={"plate_kg": 4, "dumbbell_kg": 6}) == 102.0)
    assert round_weight_for_equipment(
        101.0,
        ["dumbbell", "barbell"],
        "kg",
        steps={"plate_kg": 4, "dumbbell_kg": 6},
    ) == pytest.approx(100.0)


def test_round_weight_lbs_unit_rounds_via_pounds():
    # Путь unit="lbs" отдельно не проверялся ни одним тестом. Округление идёт
    # в фунтах (plate_lb = 5 по умолчанию) и переводится обратно в кг.
    #
    # 52.0 кг -> фунты: 52.0 * 2.2046226218 = 114.6403763336 lb
    # round(114.6403763336 / 5) * 5 = round(22.92807526...) * 5 = 23 * 5 = 115 lb
    # обратно в кг: 115 * 0.45359237 = 52.16312255 -> round(..., 2) = 52.16 кг
    #
    # В kg-режиме тот же вес даёт другое число — round_weight_for_equipment(52.0,
    # ["barbell"], "kg") == 52.5 — значит тест реально дискриминирует lb-путь,
    # а не случайно совпадает с ним.
    assert round_weight_for_equipment(52.0, ["barbell"], "lbs") == pytest.approx(
        52.16, abs=0.01
    )


def test_round_weight_falls_back_when_resolved_step_is_zero():
    # Защитный фолбэк: если резолвнутый шаг <= 0 (например, кривой steps
    # override), round_weight_for_equipment подменяет его на (2.5, "kg").
    # steps={"plate_kg": 0} заставляет _resolve_step вернуть шаг 0 для штанги.
    #
    # После фолбэка: round(101.0 / 2.5) * 2.5 = round(40.4) * 2.5 = 40 * 2.5 = 100.0 кг
    assert round_weight_for_equipment(
        101.0, ["barbell"], "kg", steps={"plate_kg": 0}
    ) == pytest.approx(100.0)


def test_round_weight_unknown_equipment_uses_default_step():
    assert round_weight_for_equipment(101.0, ["bodyweight"], "kg") == pytest.approx(
        100.0
    )


@pytest.mark.parametrize(
    "level,expected",
    [("beginner", 1.03), ("intermediate", 1.02), ("advanced", 1.01)],
)
def test_progression_factor_by_level(level, expected):
    assert progression_factor_for(level, None) == pytest.approx(expected)


def test_progression_factor_settings_override_wins():
    assert progression_factor_for("advanced", {"progression_factor": 1.05}) == pytest.approx(
        1.05
    )


@pytest.mark.parametrize("raw", ["не число", None, -1, 0])
def test_progression_factor_ignores_broken_override(raw):
    # Битый или неположительный override игнорируется, берётся уровень.
    assert progression_factor_for("advanced", {"progression_factor": raw}) == pytest.approx(
        1.01
    )


def test_progression_factor_unknown_level_uses_fallback():
    assert progression_factor_for("alien", None) == pytest.approx(1.03)
