import asyncio
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from api.services.exercise_pattern_tags import ExerciseAction
from api.schemas.plan import GeneratePlanRequest, GenerateConfig, GenerationComparison
import api.routers.plans as plans_mod


def _ex(id, muscle, tier=2, action=ExerciseAction.push):
    return SimpleNamespace(id=id, name=f"ex{id}", action=action, fatigue_tier=tier,
                           equipment_needed=["dumbbell"], main_muscle_group=muscle,
                           secondary_muscle_groups=[], source="default", app_user_id=None)


def test_generate_returns_days_for_active_split():
    profile = SimpleNamespace(experience_level="intermediate",
                              settings={"locations": ["gym"], "prehab_flags": []},
                              volume_budget={"weekly_targets": {}})
    day_bp = SimpleNamespace(name="Push", template_type="push",
                             muscle_targets=[SimpleNamespace(muscle_group_id="chest")])
    slot = SimpleNamespace(day_order=1, day=day_bp)
    blueprint = SimpleNamespace(
        id=uuid.uuid4(), name="Push split", length_days=1, slots=[slot],
    )
    pool = [_ex(1, "Грудь"), _ex(2, "Трицепс", tier=3, action=ExerciseAction.extension)]

    async def fake_targets(db, uid, day_name):
        return {"chest": {"target_sets": 4, "max_session_cap": 8},
                "triceps": {"target_sets": 3, "max_session_cap": 8}}

    req = GeneratePlanRequest(blueprint_id=None, config=GenerateConfig(seed=1))
    with patch.object(plans_mod, "_load_generation_context",
                      AsyncMock(return_value=(profile, blueprint, pool))), \
         patch.object(plans_mod.VolumeService, "calculate_session_targets",
                      side_effect=fake_targets), \
         patch.object(plans_mod, "_generation_comparison", AsyncMock(return_value=
                      GenerationComparison(applied_from=date.today(), mode="full"))):
        current_user = SimpleNamespace(id=1)
        resp = asyncio.run(plans_mod.generate_plan(req, db=object(), current_user=current_user))
    assert len(resp.days) == 1
    # day_tag is now the day NAME (for scheduler matching), not the template value
    assert resp.days[0].day_tag == "Push"
    assert len(resp.days[0].exercises) >= 1
    assert resp.inputs.split_name == "Push split"
    assert resp.inputs.mode == "full"


def test_generate_single_day_filters_by_name():
    profile = SimpleNamespace(experience_level="intermediate",
                              settings={"locations": ["gym"], "prehab_flags": []},
                              volume_budget={"weekly_targets": {}})
    push = SimpleNamespace(name="Push", template_type="push",
                           muscle_targets=[SimpleNamespace(muscle_group_id="chest")])
    pull = SimpleNamespace(name="Pull", template_type="pull",
                           muscle_targets=[SimpleNamespace(muscle_group_id="lats")])
    blueprint = SimpleNamespace(
        id=uuid.uuid4(), name="PPL", length_days=2,
        slots=[SimpleNamespace(day_order=1, day=push),
               SimpleNamespace(day_order=2, day=pull)],
    )
    pool = [_ex(1, "Грудь"), _ex(2, "Широчайшие", action=ExerciseAction.pull)]

    async def fake_targets(db, uid, day_name):
        if day_name == "Push":
            return {"chest": {"target_sets": 4, "max_session_cap": 8}}
        return {"lats": {"target_sets": 4, "max_session_cap": 8}}

    req = GeneratePlanRequest(blueprint_id=None, day_name="Pull", config=GenerateConfig(seed=1))
    with patch.object(plans_mod, "_load_generation_context",
                      AsyncMock(return_value=(profile, blueprint, pool))), \
         patch.object(plans_mod.VolumeService, "calculate_session_targets",
                      side_effect=fake_targets), \
         patch.object(plans_mod, "_generation_comparison", AsyncMock(return_value=
                      GenerationComparison(applied_from=date.today(), mode="single_day"))):
        resp = asyncio.run(plans_mod.generate_plan(req, db=object(), current_user=SimpleNamespace(id=1)))
    assert len(resp.days) == 1
    assert resp.days[0].day_tag == "Pull"
    assert resp.inputs.mode == "single_day"


def test_duration_is_not_applied_before_exercise_selection():
    from api.services.exercise_selection_engine import SelectionConfig, configured_targets

    configured = configured_targets(
        {"chest": 8, "triceps": 4},
        SelectionConfig(accent_muscle="chest", duration_minutes=30),
    )

    assert sum(configured.values()) == 16
    assert configured["chest"] > configured["triceps"]
