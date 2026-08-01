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
