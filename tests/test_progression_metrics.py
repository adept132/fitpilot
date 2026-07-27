"""Метрики и округление движка прогрессии.

Поведение обязано совпадать с autoprogression.py: перенос, а не переписывание.
"""

import pytest

from api.services import autoprogression as legacy
from api.services.progression import metrics, rounding


@pytest.mark.parametrize(
    "weight,reps,rir",
    [(100.0, 10, 2), (60.0, 5, 0), (42.5, 12, 3), (20.0, 15, 1)],
)
def test_e1rm_matches_legacy(weight, reps, rir):
    assert metrics.e1rm(weight, reps, rir) == pytest.approx(
        legacy.set_target_value(weight, reps, rir)
    )


@pytest.mark.parametrize(
    "weight,reps,rir",
    [(100.0, 10, 2), (60.0, 5, 0), (42.5, 12, 3)],
)
def test_weight_for_e1rm_is_inverse(weight, reps, rir):
    target = metrics.e1rm(weight, reps, rir)
    assert metrics.weight_for_e1rm(target, reps, rir) == pytest.approx(weight)


def test_effort_to_rir_matches_legacy():
    for level in ["warmup", "light", "medium", "prefailure", "failure", None, "junk"]:
        assert metrics.effort_to_rir(level) == legacy.effort_to_rir(level)


@pytest.mark.parametrize(
    "weight,equipment",
    [(41.3, ["barbell"]), (13.1, ["dumbbell"]), (28.0, ["block_machine"])],
)
def test_round_to_step_matches_legacy(weight, equipment):
    assert rounding.round_to_step(weight, equipment, "kg", None) == pytest.approx(
        legacy.round_weight_for_equipment(weight, equipment, "kg", None)
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
    # 30 кг минус 10 % = 27 кг. Округление к ближайшему вернуло бы 30 —
    # снижение бы не сработало вовсе.
    reduced = rounding.round_down_to_step(27.0, ["block_machine"], "kg", None)
    assert reduced < 30.0


def test_round_down_floor_is_one_step():
    assert rounding.round_down_to_step(0.4, ["barbell"], "kg", None) == pytest.approx(2.5)


def test_legacy_names_still_importable():
    # Старые имена остаются на один релиз: их импортируют fatigue/, csv_format.py.
    assert legacy.set_target_value(100.0, 10, 2) == pytest.approx(140.0)
    assert legacy.round_weight_for_equipment(41.3, ["barbell"], "kg", None) > 0
