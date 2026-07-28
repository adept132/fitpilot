"""Метрики и округление движка прогрессии.

Формулы сверяются с посчитанными вручную ожидаемыми значениями, а не с
legacy-реэкспортом: после переноса имена в autoprogression.py — это те же
объекты функций, что и здесь, поэтому сравнение с legacy само с собой ничего
не защищает.
"""

import pytest

from api.services import autoprogression as legacy
from api.services.progression import metrics, rounding


@pytest.mark.parametrize(
    "weight,reps,rir,expected",
    [
        (100.0, 10, 2, 140.0),
        (60.0, 5, 0, 70.0),
        (42.5, 12, 3, 63.75),
        (20.0, 15, 1, 30.666666666666664),
    ],
)
def test_e1rm_matches_manual_calculation(weight, reps, rir, expected):
    assert metrics.e1rm(weight, reps, rir) == pytest.approx(expected)


@pytest.mark.parametrize(
    "weight,reps,rir",
    [(100.0, 10, 2), (60.0, 5, 0), (42.5, 12, 3)],
)
def test_weight_for_e1rm_is_inverse(weight, reps, rir):
    target = metrics.e1rm(weight, reps, rir)
    assert metrics.weight_for_e1rm(target, reps, rir) == pytest.approx(weight)


@pytest.mark.parametrize(
    "level,expected",
    [
        ("warmup", 4),
        ("light", 3),
        ("medium", 2),
        ("prefailure", 1),
        ("failure", 0),
        (None, 2),
        ("junk", 2),
    ],
)
def test_effort_to_rir_matches_manual_table(level, expected):
    assert metrics.effort_to_rir(level) == expected


@pytest.mark.parametrize(
    "weight,equipment,expected",
    [
        (41.3, ["barbell"], 42.5),
        (13.1, ["dumbbell"], 12.5),
        (28.0, ["block_machine"], 27.22),
    ],
)
def test_round_to_step_matches_manual_calculation(weight, equipment, expected):
    assert rounding.round_to_step(weight, equipment, "kg", None) == pytest.approx(
        expected
    )


def test_step_kg_for_barbell_is_default_plate_step():
    assert rounding.step_kg(["barbell"], "kg", None) == pytest.approx(2.5)


def test_step_kg_for_block_machine_is_ten_pounds():
    # Блочный тренажёр всегда в фунтах: 10 lb ~ 4.54 кг.
    assert rounding.step_kg(["block_machine"], "kg", None) == pytest.approx(4.54, abs=0.02)


def test_round_down_never_exceeds_input():
    for weight in [41.3, 40.0, 39.9, 13.1, 28.0]:
        result = rounding.round_down_to_step(weight, ["barbell"], "kg", None)
        assert result <= weight + 1e-9


def test_round_down_on_block_actually_reduces():
    # 30 кг: округление к ближайшему уходит вверх (шаг 10 lb -> 31.75 кг) и
    # снижение бы не сработало вовсе. round_down обязан строго уменьшить вес.
    nearest = rounding.round_to_step(30.0, ["block_machine"], "kg", None)
    down = rounding.round_down_to_step(30.0, ["block_machine"], "kg", None)
    assert down == pytest.approx(27.22)
    assert nearest == pytest.approx(31.75)
    assert down < 30.0
    assert not (nearest < 30.0)


def test_round_down_floor_is_one_step():
    assert rounding.round_down_to_step(0.4, ["barbell"], "kg", None) == pytest.approx(2.5)


@pytest.mark.parametrize("equipment", [["bodyweight"], ["band"]])
def test_custom_step_override_applies_to_other_equipment(equipment):
    # Находка №2: оборудование вне известных категорий (plate/dumbbell/block)
    # обязано уважать пользовательский settings.weight_steps, а не жёстко
    # зашитые 5/2.5.
    default_result = rounding.round_to_step(12.0, equipment, "kg", None)
    custom_result = rounding.round_to_step(
        12.0, equipment, "kg", {"plate_kg": 5}
    )
    assert default_result == pytest.approx(12.5)
    assert custom_result == pytest.approx(10.0)
    assert custom_result != default_result


def test_legacy_names_still_importable():
    # Старые имена остаются на один релиз: их импортируют fatigue/, csv_format.py.
    assert legacy.set_target_value(100.0, 10, 2) == pytest.approx(140.0)
    assert legacy.round_weight_for_equipment(41.3, ["barbell"], "kg", None) > 0


# --- Идемпотентность округления (находка ревью Задачи 9) ---
# round_down_to_step для шага в фунтах (блочный тренажёр — всегда lb, а
# штанга/гантели/"прочее" оборудование — в режиме unit="lbs") гоняет вес
# кг -> lb -> floor -> lb -> кг. Легитимная точка сетки, прошедшая через
# хранение в кг (round(..., 2)), при обратном переводе в фунты может стать
# чуть МЕНЬШЕ исходного значения (round(160 * KG_PER_LB, 2) = 72.57, а
# 72.57 lb-эквивалент = 159.9946, а не 160) — без допуска floor съедает
# целый шаг, и повторное применение функции сдвигает вес ещё дальше вниз.
_EQUIPMENT_CATEGORIES = [
    ["barbell"],
    ["dumbbell"],
    ["block_machine"],
    ["smith"],
    ["unknown_category_xyz"],  # оборудование вне известных категорий
]


def _weight_sweep():
    """5..200 кг с мелким шагом плюс точки, полученные обратной
    конвертацией из круглых значений в фунтах — именно они ломались."""
    weights = []
    w = 5.0
    while w <= 200.0:
        weights.append(round(w, 2))
        w += 0.37
    for k in range(1, 400):
        weights.append(round(k * 10.0 * rounding.KG_PER_LB, 2))
    return weights


@pytest.mark.parametrize("equipment", _EQUIPMENT_CATEGORIES)
def test_round_down_to_step_is_idempotent(equipment):
    for weight in _weight_sweep():
        for unit in ("kg", "lbs"):
            once = rounding.round_down_to_step(weight, equipment, unit, None)
            twice = rounding.round_down_to_step(once, equipment, unit, None)
            assert twice == pytest.approx(once), (
                f"round_down_to_step не идемпотентна для {weight} кг, "
                f"equipment={equipment}, unit={unit}: {once} -> {twice}"
            )


@pytest.mark.parametrize("equipment", _EQUIPMENT_CATEGORIES)
def test_round_to_step_is_idempotent(equipment):
    for weight in _weight_sweep():
        for unit in ("kg", "lbs"):
            once = rounding.round_to_step(weight, equipment, unit, None)
            twice = rounding.round_to_step(once, equipment, unit, None)
            assert twice == pytest.approx(once), (
                f"round_to_step не идемпотентна для {weight} кг, "
                f"equipment={equipment}, unit={unit}: {once} -> {twice}"
            )
