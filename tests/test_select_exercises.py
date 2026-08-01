from types import SimpleNamespace
from api.services.exercise_pattern_tags import ExerciseAction
from api.services.exercise_selection_engine import (
    select_exercises, SelectionConfig, SelectionPolicy, _split_sets, _allocate,
)


def ex(id, muscle, tier=2, action=ExerciseAction.push, sec=None,
       equip_=("dumbbell",), category="Изолирующее", vector=None):
    return SimpleNamespace(id=id, name=f"ex{id}", action=action, fatigue_tier=tier,
                           equipment_needed=list(equip_), main_muscle_group=muscle,
                           secondary_muscle_groups=sec or [], category=category,
                           vector=vector)


def test_split_sets_chunks_are_2_to_4_and_sum():
    for total in range(2, 21):
        chunks = _split_sets(total)
        assert sum(chunks) == total
        assert all(2 <= c <= 4 for c in chunks)
        assert chunks  # non-empty for total >= 2


def test_allocate_small_target_is_single_compound():
    comp, iso = _allocate(4)
    assert comp == [4] and iso == []


def test_allocate_large_target_is_roughly_2_to_1():
    comp, iso = _allocate(9)  # 6 compound : 3 isolation
    assert sum(comp) == 6 and sum(iso) == 3


def test_no_single_set_exercises_for_any_target():
    pool = [ex(i, "Грудь", category="Базовое") for i in range(1, 4)] + \
           [ex(i, "Грудь", tier=3) for i in range(10, 14)]
    for t in range(2, 13):
        out = select_exercises({"chest": t}, pool, None, [], SelectionConfig(seed=1))
        assert all(o.sets >= 2 for o in out), f"target {t} produced a <2-set exercise"


def test_target_closed_with_direct_sets():
    pool = [ex(i, "Грудь", category="Базовое") for i in range(1, 4)] + \
           [ex(i, "Грудь", tier=3) for i in range(10, 14)]
    out = select_exercises({"chest": 6}, pool, None, [], SelectionConfig(seed=1))
    assert sum(o.sets for o in out) == 6


def test_single_compound_preferred_for_small_target():
    pool = [ex(1, "Грудь", tier=2, category="Базовое"),
            ex(2, "Грудь", tier=3, category="Изолирующее")]
    out = select_exercises({"chest": 4}, pool, None, [], SelectionConfig(seed=1))
    assert len(out) == 1
    assert out[0].sets == 4
    assert out[0].exercise_id == 1  # compound over isolation


def test_base_isolation_ratio_2_to_1():
    pool = [ex(1, "Квадрицепсы", tier=2, category="Базовое"),
            ex(2, "Квадрицепсы", tier=2, category="Базовое"),
            ex(3, "Квадрицепсы", tier=3, category="Изолирующее"),
            ex(4, "Квадрицепсы", tier=3, category="Изолирующее")]
    out = select_exercises({"quads": 9}, pool, None, [], SelectionConfig(seed=1))
    comp = sum(o.sets for o in out if o.exercise_id in (1, 2))
    iso = sum(o.sets for o in out if o.exercise_id in (3, 4))
    assert comp == 6 and iso == 3


def test_strict_ascending_fatigue_tier():
    pool = [ex(1, "Грудь", tier=3, category="Изолирующее"),
            ex(2, "Квадрицепсы", tier=1, action=ExerciseAction.squat,
               equip_=("barbell",), category="Базовое"),
            ex(3, "Бицепс", tier=2, category="Базовое")]
    out = select_exercises({"chest": 3, "quads": 3, "biceps": 3}, pool, None, [],
                           SelectionConfig(seed=1))
    tiers = [o.fatigue_tier for o in out]
    assert tiers == sorted(tiers)


def test_axial_cap_limits_heavy_squat_hinge():
    pool = [ex(1, "Квадрицепсы", 1, action=ExerciseAction.squat,
               equip_=("barbell",), category="Базовое"),
            ex(2, "Квадрицепсы", 1, action=ExerciseAction.hinge,
               equip_=("barbell",), category="Базовое"),
            ex(3, "Квадрицепсы", 2, action=ExerciseAction.squat,
               equip_=("block_machine",), category="Базовое")]
    out = select_exercises({"quads": 9}, pool, None, [],
                           SelectionConfig(seed=1), SelectionPolicy(axial_cap=1))
    axial_used = [o for o in out if o.fatigue_tier == 1]
    assert len(axial_used) <= 1


def test_deterministic_with_seed():
    pool = [ex(i, "Грудь", 2) for i in range(1, 6)]
    a = select_exercises({"chest": 6}, pool, None, [], SelectionConfig(seed=42), SelectionPolicy())
    b = select_exercises({"chest": 6}, pool, None, [], SelectionConfig(seed=42), SelectionPolicy())
    assert [o.exercise_id for o in a] == [o.exercise_id for o in b]


def test_superset_ids_deterministic_with_seed():
    pool = [ex(1, "Бицепс", 3, action=ExerciseAction.flexion),
            ex(2, "Трицепс", 3, action=ExerciseAction.extension)]
    targets = {"biceps": 3, "triceps": 3}
    a = select_exercises(targets, pool, None, [],
                         SelectionConfig(seed=7, use_supersets=True), SelectionPolicy())
    b = select_exercises(targets, pool, None, [],
                         SelectionConfig(seed=7, use_supersets=True), SelectionPolicy())
    assert [(o.exercise_id, o.superset_group_id) for o in a] == \
           [(o.exercise_id, o.superset_group_id) for o in b]
    assert any(o.superset_group_id for o in a)


def test_tiny_target_closed_with_two_sets():
    assert _split_sets(1) == [2]
    pool = [ex(1, "Передняя дельта", tier=3, category="Изолирующее")]
    out = select_exercises({"front_delts": 1}, pool, None, [], SelectionConfig(seed=1))
    assert len(out) == 1 and out[0].sets == 2


def test_no_tier_is_a_majority_when_pool_allows():
    # Enough tier variety for chest -> the balance rule should be satisfiable.
    pool = [ex(1, "Грудь", tier=1, category="Базовое"),
            ex(2, "Грудь", tier=2, category="Базовое"),
            ex(3, "Грудь", tier=3, category="Изолирующее"),
            ex(4, "Грудь", tier=3, category="Изолирующее")]
    out = select_exercises({"chest": 8}, pool, None, [], SelectionConfig(seed=1))
    tc = {}
    for o in out:
        tc[o.fatigue_tier] = tc.get(o.fatigue_tier, 0) + 1
    n = len(out)
    for tier, cnt in tc.items():
        assert cnt <= n - cnt, f"tier {tier}: {cnt} > others {n - cnt}"


def test_prefers_distinct_action_vector_for_a_muscle():
    from api.services.exercise_pattern_tags import ExerciseVector
    # Two horizontal presses + one incline press; picking 2 should cover both vectors.
    pool = [ex(1, "Грудь", tier=2, category="Базовое", vector=ExerciseVector.horizontal),
            ex(2, "Грудь", tier=2, category="Базовое", vector=ExerciseVector.horizontal),
            ex(3, "Грудь", tier=2, category="Базовое", vector=ExerciseVector.incline)]
    out = select_exercises({"chest": 5}, pool, None, [], SelectionConfig(seed=1))
    ids = {o.exercise_id for o in out}
    # The distinct-vector exercise (incline) must be included rather than two horizontals.
    assert 3 in ids


def test_tier_balance_enforced_via_swap():
    # All compounds tier-1, isolations tier-2/3. Balance must pull tier-1 <= half,
    # even at the cost of the 2:1 base:isolation ratio (balance is primary/hard).
    pool = [ex(i, "Грудь", tier=1, category="Базовое") for i in range(1, 5)] + \
           [ex(i, "Грудь", tier=2, category="Изолирующее") for i in range(10, 13)] + \
           [ex(i, "Грудь", tier=3, category="Изолирующее") for i in range(20, 23)]
    out = select_exercises({"chest": 16}, pool, None, [], SelectionConfig(seed=1))
    tc = {}
    for o in out:
        tc[o.fatigue_tier] = tc.get(o.fatigue_tier, 0) + 1
    n = len(out)
    assert all(c <= n - c for c in tc.values()), f"not balanced: {tc}"
