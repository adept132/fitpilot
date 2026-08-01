from types import SimpleNamespace

from api.services.exercise_pattern_tags import ExerciseAction
from api.services.commands.schema import Command, CommandType
from api.services.commands.executor import apply


def ex(id, muscle, tier=2, action=ExerciseAction.push, category="Изолирующее",
       equipment=None):
    return SimpleNamespace(id=id, name=f"ex{id}", action=action, fatigue_tier=tier,
                           equipment_needed=equipment if equipment is not None else ["dumbbell"],
                           main_muscle_group=muscle,
                           secondary_muscle_groups=[], category=category, vector=None)


def _draft():
    return [{"exercise_id": 1, "name": "Жим", "target_sets": 4, "order_index": 0,
             "superset_group_id": None, "fatigue_tier": 1, "primary_muscle": "Грудь",
             "secondary_muscle": None}]


# ---------------------------------------------------------------------------
# Brief's 3 tests
# ---------------------------------------------------------------------------

def test_add_muscle_reselects():
    pool = [ex(1, "Грудь", 1, category="Базовое"),
            ex(10, "Бицепс", 3, action=ExerciseAction.flexion)]
    log = [Command(CommandType.ADD_MUSCLE, {"muscles": ["biceps"], "target_override": 4}, seed=1)]
    out, summaries, warns = apply(_draft(), log, pool, context={}, experience_level="intermediate")
    assert any(e["primary_muscle"] == "Бицепс" for e in out)


def test_replace_criteria_isolation():
    pool = [ex(1, "Грудь", 1, category="Базовое"),
            ex(5, "Грудь", 3, action=ExerciseAction.flexion, category="Изолирующее")]
    log = [Command(CommandType.REPLACE_EXERCISE,
           {"from_exercise_id": 1, "to": {"criteria": {"category": "isolation"}}}, seed=1)]
    out, _, _ = apply(_draft(), log, pool, context={}, experience_level="intermediate")
    ids = [e["exercise_id"] for e in out]
    assert 1 not in ids and 5 in ids


def test_hardcap_trim_warns():
    # 5 chest exercises at 4 sets = 20 -> beginner cap 6 -> trimmed with warning
    draft = [{"exercise_id": i, "name": f"e{i}", "target_sets": 4, "order_index": i,
              "superset_group_id": None, "fatigue_tier": 2, "primary_muscle": "Грудь",
              "secondary_muscle": None}
             for i in range(5)]
    log = [Command(CommandType.SET_ALL_SETS, {"n": 4}, seed=1)]
    out, _, warns = apply(draft, log, pool=[], context={}, experience_level="beginner")
    assert sum(e["target_sets"] for e in out) <= 6 and warns


# ---------------------------------------------------------------------------
# One focused test per re-select branch
# ---------------------------------------------------------------------------

def test_accent_muscle_adds_volume():
    # chest starts at 4 sets -> accent 1.5x -> target 6 -> +2 sets via a NEW exercise.
    pool = [ex(1, "Грудь", 1, category="Базовое"),
            ex(2, "Грудь", 3, category="Изолирующее")]
    log = [Command(CommandType.ACCENT_MUSCLE, {"muscles": ["chest"]}, seed=1)]
    out, _, _ = apply(_draft(), log, pool, context={}, experience_level="intermediate")
    chest = [e for e in out if e["primary_muscle"] == "Грудь"]
    assert len(chest) == 2                       # a new exercise was added
    assert sum(e["target_sets"] for e in chest) == 6
    assert all(2 <= e["target_sets"] <= 4 for e in chest)


def test_adjust_positive_fills_then_reselects():
    # one chest exercise at 2 sets, +3 -> existing to MAX (4, +2), leftover 1 -> new ex.
    draft = [{"exercise_id": 1, "name": "Жим", "target_sets": 2, "order_index": 0,
              "superset_group_id": None, "fatigue_tier": 2, "primary_muscle": "Грудь",
              "secondary_muscle": None}]
    pool = [ex(1, "Грудь", 2, category="Базовое"),
            ex(2, "Грудь", 3, category="Изолирующее")]
    log = [Command(CommandType.ADJUST_MUSCLE_SETS, {"muscles": ["chest"], "delta": 3}, seed=1)]
    out, _, _ = apply(draft, log, pool, context={}, experience_level="intermediate")
    chest = [e for e in out if e["primary_muscle"] == "Грудь"]
    first = [e for e in chest if e["exercise_id"] == 1][0]
    assert first["target_sets"] == 4             # existing filled to MAX_SETS
    assert len(chest) == 2                        # leftover set seeded a new exercise
    assert sum(e["target_sets"] for e in chest) == 6


def test_equipment_only_reselects_violator():
    # draft chest needs a dumbbell; restrict to barbell -> violator swapped for barbell ex.
    pool = [ex(1, "Грудь", 1, category="Базовое", equipment=["dumbbell"]),
            ex(2, "Грудь", 2, category="Базовое", equipment=["barbell"])]
    log = [Command(CommandType.EQUIPMENT_CONSTRAINT,
                   {"mode": "only", "equipment": ["barbell"]}, seed=1)]
    out, _, _ = apply(_draft(), log, pool, context={}, experience_level="intermediate")
    ids = [e["exercise_id"] for e in out]
    assert 1 not in ids and 2 in ids
    assert all(e["primary_muscle"] == "Грудь" for e in out)
    assert out[[e["exercise_id"] for e in out].index(2)]["target_sets"] == 4  # sets preserved


def test_add_injury_reselects_forbidden_action():
    # lower_back injury forbids the hinge action -> hinge exercise swapped out.
    pool = [ex(1, "Грудь", 1, action=ExerciseAction.hinge, category="Базовое"),
            ex(2, "Грудь", 2, action=ExerciseAction.push, category="Базовое")]
    log = [Command(CommandType.ADD_INJURY, {"flag": "lower_back"}, seed=1)]
    out, _, _ = apply(_draft(), log, pool, context={}, experience_level="intermediate")
    ids = [e["exercise_id"] for e in out]
    assert 1 not in ids and 2 in ids
    assert all(e["primary_muscle"] == "Грудь" for e in out)


def test_base_iso_ratio_reallocates():
    # total 6 sets; ratio 0.5 -> compound_fraction 1/3 -> compound=2 sets, iso=4 sets.
    draft = [
        {"exercise_id": 1, "name": "База", "target_sets": 4, "order_index": 0,
         "superset_group_id": None, "fatigue_tier": 1, "primary_muscle": "Грудь", "secondary_muscle": None},
        {"exercise_id": 2, "name": "Изоляция", "target_sets": 2, "order_index": 1,
         "superset_group_id": None, "fatigue_tier": 3, "primary_muscle": "Грудь", "secondary_muscle": None},
    ]
    pool = [ex(1, "Грудь", 1, category="Базовое"),
            ex(2, "Грудь", 3, category="Изолирующее")]
    log = [Command(CommandType.SET_BASE_ISO_RATIO, {"ratio": 0.5}, seed=1)]
    out, _, _ = apply(draft, log, pool, context={}, experience_level="intermediate")
    by_id = {e["exercise_id"]: e for e in out}
    assert sum(e["target_sets"] for e in out) == 6          # total volume preserved
    assert by_id[1]["target_sets"] == 2                     # compound gets fewer
    assert by_id[2]["target_sets"] == 4                     # isolation gets more


def test_replay_is_deterministic():
    pool = [ex(1, "Грудь", 1, category="Базовое"),
            ex(10, "Бицепс", 3, action=ExerciseAction.flexion),
            ex(11, "Бицепс", 2, action=ExerciseAction.flexion, category="Базовое")]
    log = [Command(CommandType.ADD_MUSCLE, {"muscles": ["biceps"], "target_override": 6}, seed=7)]
    a, _, _ = apply(_draft(), log, pool, context={}, experience_level="intermediate")
    b, _, _ = apply(_draft(), log, pool, context={}, experience_level="intermediate")
    assert a == b
