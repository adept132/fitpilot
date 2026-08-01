from types import SimpleNamespace
from api.services.exercise_pattern_tags import ExerciseAction
from api.services.exercise_selection_engine import (
    is_axial, superset_eligible, filter_pool,
)


def ex(**kw):
    base = dict(id=1, name="x", action=ExerciseAction.push, fatigue_tier=2,
                equipment_needed=["dumbbell"], main_muscle_group="Грудь",
                secondary_muscle_groups=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_is_axial_true_only_for_heavy_squat_or_hinge():
    assert is_axial(ex(action=ExerciseAction.squat, fatigue_tier=1)) is True
    assert is_axial(ex(action=ExerciseAction.hinge, fatigue_tier=1)) is True
    assert is_axial(ex(action=ExerciseAction.squat, fatigue_tier=2)) is False
    assert is_axial(ex(action=ExerciseAction.push, fatigue_tier=1)) is False


def test_superset_eligible_only_tier3_non_axial():
    assert superset_eligible(ex(fatigue_tier=3, action=ExerciseAction.flexion)) is True
    assert superset_eligible(ex(fatigue_tier=1, action=ExerciseAction.flexion)) is False
    assert superset_eligible(ex(fatigue_tier=3, action=ExerciseAction.squat)) is False


def test_filter_pool_removes_forbidden_injury_actions():
    pool = [ex(id=1, action=ExerciseAction.hinge, fatigue_tier=1),
            ex(id=2, action=ExerciseAction.push)]
    out = filter_pool(pool, allowed_equipment_keys=None, prehab_flags=["lower_back"])
    assert [e.id for e in out] == [2]  # hinge forbidden by lower_back


def test_filter_pool_equipment_gate_with_bodyweight_fallback():
    barbell = ex(id=1, equipment_needed=["barbell"])
    bw = ex(id=2, equipment_needed=[])  # empty -> treated as bodyweight
    out = filter_pool([barbell, bw], allowed_equipment_keys={"bodyweight"}, prehab_flags=[])
    assert [e.id for e in out] == [2]
