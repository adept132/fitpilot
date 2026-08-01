import random
from api.services.exercise_selection_engine import SelectedExercise, group_supersets


def sel(id, tier, primary, sec=None):
    return SelectedExercise(exercise_id=id, name=f"e{id}", sets=3, order_index=id,
                            superset_group_id=None, fatigue_tier=tier,
                            primary_muscle=primary, secondary_muscle=sec)


def test_pairs_two_isolation_non_overlapping():
    items = [sel(1, 3, "Бицепс"), sel(2, 3, "Трицепс")]
    out = group_supersets(items, random.Random(1))
    ids = {o.superset_group_id for o in out}
    assert None not in ids and len(ids) == 1  # both share one group


def test_never_supersets_tier1_or_overlap():
    items = [sel(1, 1, "Квадрицепсы"), sel(2, 3, "Бицепс"), sel(3, 3, "Бицепс")]
    out = group_supersets(items, random.Random(1))
    # tier1 stays solo; the two biceps overlap -> stay solo
    assert all(o.superset_group_id is None for o in out)


def test_forms_multiple_supersets_partners_adjacent():
    # Four non-heavy, non-overlapping exercises -> two supersets, partners adjacent.
    items = [sel(1, 2, "Грудь"), sel(2, 2, "Средняя часть спины"),
             sel(3, 3, "Бицепс"), sel(4, 3, "Трицепс")]
    out = group_supersets(items, random.Random(5))
    groups = {}
    for o in out:
        if o.superset_group_id:
            groups.setdefault(o.superset_group_id, []).append(o)
    assert len(groups) == 2  # more than just the last pair
    for members in groups.values():
        idxs = sorted(o.order_index for o in members)
        assert idxs[-1] - idxs[0] == len(idxs) - 1  # members are consecutive


def test_supersets_via_select_exercises_are_adjacent():
    from types import SimpleNamespace
    from api.services.exercise_pattern_tags import ExerciseAction
    from api.services.exercise_selection_engine import select_exercises, SelectionConfig, SelectionPolicy

    def ex(id, muscle, tier, action=ExerciseAction.push):
        return SimpleNamespace(id=id, name=f"ex{id}", action=action, fatigue_tier=tier,
                               equipment_needed=["dumbbell"], main_muscle_group=muscle,
                               secondary_muscle_groups=[], category="Изолирующее", vector=None)

    pool = [ex(1, "Бицепс", 3, ExerciseAction.flexion),
            ex(2, "Трицепс", 3, ExerciseAction.extension),
            ex(3, "Средняя дельта", 2, ExerciseAction.abduction),
            ex(4, "Икры", 2, ExerciseAction.plantarflexion)]
    targets = {"biceps": 3, "triceps": 3, "side_delts": 3, "calves": 3}
    out = select_exercises(targets, pool, None, [],
                           SelectionConfig(seed=3, use_supersets=True), SelectionPolicy())
    groups = {}
    for o in out:
        if o.superset_group_id:
            groups.setdefault(o.superset_group_id, []).append(o.order_index)
    assert len(groups) >= 2
    for idxs in groups.values():
        idxs = sorted(idxs)
        assert idxs[-1] - idxs[0] == len(idxs) - 1
