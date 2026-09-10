from types import SimpleNamespace

from api.services.exercise_selection_engine import SelectedExercise
from api.services.plan_duration import (
    DurationConfig,
    estimate_duration_seconds,
    fit_to_duration,
    preparation_seconds,
    rest_seconds,
    set_execution_seconds,
)


def selected(id: int, sets: int, tier: int = 2, group: str | None = None, muscle: str = "Грудь"):
    return SelectedExercise(
        exercise_id=id, name=f"ex{id}", sets=sets, order_index=id,
        superset_group_id=group, fatigue_tier=tier, primary_muscle=muscle,
        secondary_muscle=None,
    )


def pool(id: int, equipment: str = "dumbbell"):
    return SimpleNamespace(id=id, equipment_needed=[equipment])


def test_time_primitives_match_product_model():
    assert preparation_seconds(pool(1, "bodyweight")) == 30
    assert preparation_seconds(pool(1, "block_machine")) == 45
    assert preparation_seconds(pool(1, "dumbbell")) == 60
    assert preparation_seconds(pool(1, "free_machine")) == 90
    assert preparation_seconds(pool(1, "barbell")) == 120
    assert rest_seconds(1, DurationConfig(timer_mode="smart", day_effort="medium")) == 180
    assert rest_seconds(3, DurationConfig(timer_mode="fixed", fixed_rest_seconds=75)) == 75
    assert set_execution_seconds(1, "medium") == 42


def test_transition_overlaps_previous_rest_with_next_setup():
    exercises = [selected(1, 2, tier=2), selected(2, 2, tier=3)]
    pools = {1: pool(1, "dumbbell"), 2: pool(2, "barbell")}
    cfg = DurationConfig(timer_mode="fixed", fixed_rest_seconds=120)
    # startup + setup + tier-2 sets/rest, then transition overlapping the
    # previous rest with barbell setup, then tier-3 sets/rest.
    expected = 90 + 60 + 2 * 45 + 120 + 120 + 2 * 55 + 120
    assert estimate_duration_seconds(exercises, pools, cfg) == expected


def test_superset_rests_once_per_round():
    exercises = [selected(1, 3, tier=2, group="g"), selected(2, 3, tier=3, group="g")]
    pools = {1: pool(1), 2: pool(2, "bodyweight")}
    cfg = DurationConfig(timer_mode="fixed", fixed_rest_seconds=60)
    # startup + both setups + 3 rounds; 15 sec partner switch and only 2 rests.
    expected = 90 + 90 + 3 * (45 + 55 + 15) + 2 * 60
    assert estimate_duration_seconds(exercises, pools, cfg) == expected


def test_triple_superset_keeps_one_rest_per_round():
    exercises = [
        selected(1, 3, tier=2, group="g"),
        selected(2, 3, tier=3, group="g"),
        selected(3, 3, tier=3, group="g"),
    ]
    pools = {1: pool(1), 2: pool(2, "bodyweight"), 3: pool(3, "bodyweight")}
    cfg = DurationConfig(timer_mode="fixed", fixed_rest_seconds=60)
    # Three executions, two 15-second switches, one rest after each non-final round.
    expected = 90 + 120 + 3 * (45 + 55 + 55 + 30) + 2 * 60
    assert estimate_duration_seconds(exercises, pools, cfg) == expected


def test_fitting_reduces_sets_and_reports_unreachable_minimum():
    exercises = [selected(1, 4, tier=1), selected(2, 4, tier=2, muscle="Трицепс")]
    pools = {1: pool(1, "barbell"), 2: pool(2, "dumbbell")}
    cfg = DurationConfig(timer_mode="smart", day_effort="medium")

    result = fit_to_duration(exercises, pools, 20, cfg)

    assert sum(row.sets for row in result.exercises) < 8
    assert result.estimated_seconds <= 20 * 60
    assert result.limit_met is True

    unreachable = fit_to_duration(exercises, pools, 1, cfg)
    assert unreachable.limit_met is False
    assert all(row.sets >= 2 for row in unreachable.exercises)
