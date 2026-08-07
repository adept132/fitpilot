"""Чистая мера объёма: вклад упражнения по мышцам."""
import pytest

from api.services.volume.measure import (
    INDIRECT_WEIGHT,
    MuscleContribution,
    accumulate,
    apply_adjustments,
    contribution,
)


def test_direct_muscle_gets_full_weight():
    result = contribution("Грудь", [], 3)
    assert result == {"chest": MuscleContribution(direct=3.0, indirect=0.0)}


def test_secondary_muscles_get_half_weight():
    result = contribution("Грудь", ["Трицепс", "Передняя дельта"], 4)
    assert result["chest"] == MuscleContribution(direct=4.0, indirect=0.0)
    assert result["triceps"] == MuscleContribution(direct=0.0, indirect=4.0)
    assert result["front_delts"] == MuscleContribution(direct=0.0, indirect=4.0)


def test_effective_applies_indirect_weight():
    assert MuscleContribution(direct=4.0, indirect=6.0).effective == pytest.approx(
        4.0 + 6.0 * INDIRECT_WEIGHT
    )


def test_uppercase_ui_codes_are_normalized():
    result = contribution("LATISSIMUS", ["POSTERIOR_DELT"], 2)
    assert set(result) == {"lats", "rear_delts"}


def test_unknown_muscle_is_dropped_not_raised():
    result = contribution("Хвост", ["Жабры"], 5)
    assert result == {}


def test_muscle_listed_both_primary_and_secondary_counts_once_as_direct():
    # Каталог местами дублирует главную мышцу в secondary. Двойной учёт
    # завысил бы объём ровно у самых частых базовых движений.
    result = contribution("Грудь", ["Грудь", "Трицепс"], 3)
    assert result["chest"] == MuscleContribution(direct=3.0, indirect=0.0)
    assert result["triceps"] == MuscleContribution(direct=0.0, indirect=3.0)


def test_zero_sets_yields_nothing():
    assert contribution("Грудь", ["Трицепс"], 0) == {}


def test_none_inputs_yield_nothing():
    assert contribution(None, None, 3) == {}


def test_accumulate_sums_both_channels():
    total = {"chest": MuscleContribution(direct=3.0, indirect=1.0)}
    part = {
        "chest": MuscleContribution(direct=2.0, indirect=0.0),
        "lats": MuscleContribution(direct=0.0, indirect=4.0),
    }
    merged = accumulate(total, part)
    assert merged["chest"] == MuscleContribution(direct=5.0, indirect=1.0)
    assert merged["lats"] == MuscleContribution(direct=0.0, indirect=4.0)


def test_accumulate_does_not_mutate_inputs():
    total = {"chest": MuscleContribution(direct=3.0, indirect=0.0)}
    accumulate(total, {"chest": MuscleContribution(direct=1.0, indirect=0.0)})
    assert total["chest"] == MuscleContribution(direct=3.0, indirect=0.0)


def test_apply_adjustments_adds_sets_to_matching_exercise():
    compiled = [
        {"exercise_id": 10, "target_sets": 3},
        {"exercise_id": 11, "target_sets": 4},
    ]
    result = apply_adjustments(compiled, [{"exercise_id": 10, "delta_sets": 2}])
    assert result[0]["target_sets"] == 5
    assert result[1]["target_sets"] == 4


def test_apply_adjustments_cuts_but_never_below_one():
    # Ноль подходов означал бы «убрать упражнение» — это другое решение,
    # с другой карточкой и другими последствиями для истории прогрессии.
    compiled = [{"exercise_id": 10, "target_sets": 2}]
    result = apply_adjustments(compiled, [{"exercise_id": 10, "delta_sets": -5}])
    assert result[0]["target_sets"] == 1


def test_apply_adjustments_ignores_unknown_exercise():
    compiled = [{"exercise_id": 10, "target_sets": 3}]
    result = apply_adjustments(compiled, [{"exercise_id": 999, "delta_sets": 2}])
    assert result[0]["target_sets"] == 3


def test_apply_adjustments_without_adjustments_returns_equal_list():
    compiled = [{"exercise_id": 10, "target_sets": 3}]
    assert apply_adjustments(compiled, None) == compiled


def test_apply_adjustments_does_not_mutate_input():
    compiled = [{"exercise_id": 10, "target_sets": 3}]
    apply_adjustments(compiled, [{"exercise_id": 10, "delta_sets": 2}])
    assert compiled[0]["target_sets"] == 3
