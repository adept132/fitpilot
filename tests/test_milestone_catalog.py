"""Каталог вех: 18 порогов по шести движениям (P1-03 ч.2, §5.1)."""
import pytest

from api.services.milestones.catalog import (
    LIFTS, MILESTONES, milestone_by_code, target_kg,
)


def test_catalog_holds_eighteen_milestones_over_six_lifts():
    assert len(MILESTONES) == 18
    assert len({m.code for m in MILESTONES}) == 18, "коды обязаны быть уникальны"
    assert len(LIFTS) == 6
    assert {m.lift for m in MILESTONES} == set(LIFTS)


def test_every_milestone_has_exactly_one_threshold_rule():
    for m in MILESTONES:
        rules = [m.absolute_kg is not None, m.bodyweight_multiple is not None]
        assert sum(rules) == 1, f"{m.code}: ровно одно правило порога"


def test_bodyweight_multiple_scales_with_bodyweight():
    squat_double = milestone_by_code("squat_2x_bw")
    assert target_kg(squat_double, 80.0) == 160.0
    assert target_kg(squat_double, 90.0) == 180.0


def test_absolute_threshold_ignores_bodyweight():
    bench_100 = milestone_by_code("bench_100kg")
    assert target_kg(bench_100, 70.0) == 100.0
    assert target_kg(bench_100, 110.0) == 100.0


def test_bodyweight_multiple_needs_bodyweight():
    """Вес тела не заполнен — относительная веха не считается (§7)."""
    assert target_kg(milestone_by_code("squat_2x_bw"), None) is None
    assert target_kg(milestone_by_code("bench_100kg"), None) == 100.0


def test_pullup_milestones_use_bodyweight_as_the_load():
    first = milestone_by_code("pullup_first")
    assert first.target_reps == 1
    assert target_kg(first, 80.0) == 80.0
    ten = milestone_by_code("pullup_10_reps")
    assert ten.target_reps == 10


def test_weighted_pullup_adds_to_bodyweight():
    plus20 = milestone_by_code("pullup_plus20")
    assert target_kg(plus20, 80.0) == 100.0


def test_unknown_code_raises():
    with pytest.raises(KeyError):
        milestone_by_code("нет такой вехи")


def test_every_lift_maps_to_a_unique_exercise_name():
    names = list(LIFTS.values())
    assert len(names) == len(set(names))
