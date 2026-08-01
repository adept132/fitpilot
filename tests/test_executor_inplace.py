from api.services.commands.schema import Command, CommandType
from api.services.commands.executor import apply_inplace


def _draft():
    return [
        {"exercise_id": 1, "name": "Жим", "target_sets": 4, "order_index": 0,
         "superset_group_id": None, "fatigue_tier": 1, "primary_muscle": "Грудь", "secondary_muscle": None},
        {"exercise_id": 2, "name": "Разведение", "target_sets": 2, "order_index": 1,
         "superset_group_id": None, "fatigue_tier": 3, "primary_muscle": "Грудь", "secondary_muscle": None},
        {"exercise_id": 3, "name": "Присед", "target_sets": 3, "order_index": 2,
         "superset_group_id": None, "fatigue_tier": 1, "primary_muscle": "Квадрицепсы", "secondary_muscle": None},
    ]


def test_exclude():
    out, _ = apply_inplace(_draft(), Command(CommandType.EXCLUDE_EXERCISE, {"exercise_id": 1}))
    assert [e["exercise_id"] for e in out] == [2, 3]


def test_set_all_scalar():
    out, _ = apply_inplace(_draft(), Command(CommandType.SET_ALL_SETS, {"n": 3}))
    assert all(e["target_sets"] == 3 for e in out)


def test_set_all_range_clamps():
    out, _ = apply_inplace(_draft(), Command(CommandType.SET_ALL_SETS, {"range": [2, 3]}))
    assert [e["target_sets"] for e in out] == [3, 2, 3]  # 4->3 clamp, 2 stays, 3 stays


def test_scale_up_clamped():
    out, _ = apply_inplace(_draft(), Command(CommandType.SCALE_VOLUME, {"factor": 1.5}))
    assert all(2 <= e["target_sets"] <= 4 for e in out)


def test_adjust_negative():
    out, _ = apply_inplace(_draft(), Command(CommandType.ADJUST_MUSCLE_SETS, {"muscles": ["chest"], "delta": -2}))
    chest = [e for e in out if e["primary_muscle"] == "Грудь"]
    assert sum(e["target_sets"] for e in chest) == 4  # was 6 -> 4


def test_replace_specific_keeps_sets():
    out, _ = apply_inplace(_draft(), Command(CommandType.REPLACE_EXERCISE,
             {"from_exercise_id": 1, "to": {"exercise_id": 99, "name": "Жим гантелей",
                                            "fatigue_tier": 2, "primary_muscle": "Грудь", "secondary_muscle": None}}))
    r = [e for e in out if e["exercise_id"] == 99][0]
    assert r["target_sets"] == 4 and r["order_index"] == 0
