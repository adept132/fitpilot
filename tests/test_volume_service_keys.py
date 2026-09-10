"""Regression: VolumeService must normalize mixed muscle-key encodings so a
Russian-named day target (system-split seed) matches the EN-keyed volume budget.
Before the fix this returned {} silently for every system split."""
import asyncio
from types import SimpleNamespace
from api.services.volume_service import VolumeService


class FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeSession:
    def __init__(self, *objs):
        self._objs = list(objs)
        self._i = 0

    async def execute(self, stmt):
        obj = self._objs[self._i]
        self._i += 1
        return FakeResult(obj)


def _day(name, template, muscle_ids):
    return SimpleNamespace(
        name=name,
        template_type=template,
        muscle_targets=[SimpleNamespace(muscle_group_id=m) for m in muscle_ids],
    )


def test_russian_day_target_matches_en_budget():
    profile = SimpleNamespace(volume_budget={
        "constraints": {"max_sets_per_session_per_muscle": 8},
        "weekly_targets": {"chest": {"target_sets": 12, "min_floor": 4}},
    })
    # System-split seed stores the Russian display name "Грудь", not "chest".
    day = _day("Push", "push", ["Грудь"])
    blueprint = SimpleNamespace(length_days=3, slots=[SimpleNamespace(day=day)])

    sess = FakeSession(profile, blueprint)
    result = asyncio.run(VolumeService.calculate_session_targets(sess, app_user_id=1, day_tag="Push"))

    # Non-empty, keyed by the EN system key.
    assert "chest" in result
    assert result["chest"]["target_sets"] >= 1


def test_uppercase_code_target_matches_en_budget():
    profile = SimpleNamespace(volume_budget={
        "constraints": {"max_sets_per_session_per_muscle": 8},
        "weekly_targets": {"front_delts": {"target_sets": 6, "min_floor": 2}},
    })
    # Custom-split builder stores the UPPERCASE code "ANTERIOR_DELT".
    day = _day("Shoulders", "custom", ["ANTERIOR_DELT"])
    blueprint = SimpleNamespace(length_days=7, slots=[SimpleNamespace(day=day)])

    sess = FakeSession(profile, blueprint)
    result = asyncio.run(VolumeService.calculate_session_targets(sess, app_user_id=1, day_tag="Shoulders"))

    assert "front_delts" in result
    assert result["front_delts"]["target_sets"] >= 1


def test_weekly_floor_is_scaled_to_session_in_short_repeating_split():
    profile = SimpleNamespace(volume_budget={
        "constraints": {"max_sets_per_session_per_muscle": 10},
        "weekly_targets": {
            "chest": {"target_sets": 15, "min_floor": 8},
            "lats": {"target_sets": 8, "min_floor": 8},
            "mid_back": {"target_sets": 8, "min_floor": 8},
            "front_delts": {"target_sets": 5, "min_floor": 5},
            "side_delts": {"target_sets": 8, "min_floor": 8},
            "rear_delts": {"target_sets": 5, "min_floor": 5},
            "biceps": {"target_sets": 14, "min_floor": 8},
            "triceps": {"target_sets": 8, "min_floor": 8},
        },
    })
    upper_muscles = list(profile.volume_budget["weekly_targets"])
    upper = _day("Upper", "upper", upper_muscles)
    lower = _day("Lower", "lower", ["quads", "hamstrings"])
    rest = _day("Rest", "active_rest", [])
    blueprint = SimpleNamespace(
        length_days=3,
        slots=[SimpleNamespace(day=upper), SimpleNamespace(day=lower), SimpleNamespace(day=rest)],
    )

    result = asyncio.run(VolumeService.calculate_session_targets(
        FakeSession(profile, blueprint), app_user_id=1, day_tag="Upper",
    ))

    # Regression: the unscaled weekly floors produced exactly 58 sets here.
    assert sum(item["target_sets"] for item in result.values()) == 28
    assert result["chest"]["target_sets"] == 6
    assert result["lats"]["target_sets"] == 3
