from types import SimpleNamespace
from api.services.exercise_pattern_tags import ExerciseAction
from api.services.exercise_selection_engine import SelectionConfig
from api.services.plan_generator_service import build_day


def ex(id, muscle, tier=2, action=ExerciseAction.push):
    return SimpleNamespace(id=id, name=f"ex{id}", action=action, fatigue_tier=tier,
                           equipment_needed=["dumbbell"], main_muscle_group=muscle,
                           secondary_muscle_groups=[])


def test_build_day_returns_exercises_and_coverage():
    pool = [ex(1, "Грудь"), ex(2, "Трицепс", tier=3, action=ExerciseAction.extension)]
    targets = {"chest": 4, "triceps": 3}
    day = build_day("push", "Push", targets, pool, None, [], "intermediate",
                    SelectionConfig(seed=1))
    assert day.day_tag == "push"
    assert len(day.exercises) >= 1
    assert set(day.coverage.keys()) == {"chest", "triceps"}


def test_build_day_warns_on_unfilled_muscle():
    pool = [ex(1, "Грудь")]  # nothing for triceps
    targets = {"chest": 4, "triceps": 3}
    day = build_day("push", "Push", targets, pool, None, [], "intermediate",
                    SelectionConfig(seed=1))
    assert any("triceps" in w for w in day.warnings)


def test_build_day_passes_validator_hard_cap():
    pool = [ex(i, "Грудь") for i in range(1, 6)]
    targets = {"chest": 6}
    day = build_day("push", "Push", targets, pool, None, [], "beginner",
                    SelectionConfig(seed=1))
    total_chest = sum(e.sets for e in day.exercises if e.primary_muscle == "Грудь")
    assert total_chest <= 6
