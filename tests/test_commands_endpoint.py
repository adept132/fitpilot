import asyncio
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import api.routers.plans as plans_mod
from api.schemas.commands import ApplyCommandsRequest


def _profile():
    return SimpleNamespace(experience_level="intermediate",
                           settings={"locations": ["gym"], "prehab_flags": []})


def test_apply_exclude_via_comment():
    profile = _profile()
    pool = []
    base = {"day_tag": "push", "day_name": "Push", "coverage": {}, "warnings": [],
            "exercises": [
                {"exercise_id": 1, "name": "Выпады", "target_sets": 3, "order_index": 0,
                 "superset_group_id": None, "fatigue_tier": 1,
                 "primary_muscle": "Квадрицепсы", "secondary_muscle": None},
                {"exercise_id": 2, "name": "Жим лёжа", "target_sets": 3, "order_index": 1,
                 "superset_group_id": None, "fatigue_tier": 1,
                 "primary_muscle": "Грудь", "secondary_muscle": None},
            ]}
    req = ApplyCommandsRequest(base_draft=base, command_log=[], new_comment="убери выпады", context={})
    with patch.object(plans_mod, "_load_commands_context",
                      AsyncMock(return_value=(profile, pool))):
        resp = asyncio.run(plans_mod.apply_commands(req, db=object(), current_user=SimpleNamespace(id=1)))
    survivors = resp.final_draft.exercises
    # (a) the excluded exercise is gone
    assert all(e.exercise_id != 1 for e in survivors)
    # (b) a survivor is a real GeneratedExerciseOut with accessible attributes.
    #     Fails if final_draft is built via model_copy(update=...), which leaves
    #     the executor's plain dicts un-coerced (no .exercise_id / .name attrs).
    assert survivors and survivors[0].exercise_id == 2 and survivors[0].name
    assert resp.reply


def test_apply_no_profile_does_not_crash():
    """A user without an AppUserProfile row must not 500 (profile=None guard)."""
    pool = []
    base = {"day_tag": "push", "day_name": "Push", "coverage": {}, "warnings": [],
            "exercises": [
                {"exercise_id": 1, "name": "Выпады", "target_sets": 3, "order_index": 0,
                 "superset_group_id": None, "fatigue_tier": 1,
                 "primary_muscle": "Квадрицепсы", "secondary_muscle": None},
            ]}
    req = ApplyCommandsRequest(base_draft=base, command_log=[], new_comment="убери выпады", context={})
    with patch.object(plans_mod, "_load_commands_context",
                      AsyncMock(return_value=(None, pool))):
        resp = asyncio.run(plans_mod.apply_commands(req, db=object(), current_user=SimpleNamespace(id=1)))
    assert all(e.exercise_id != 1 for e in resp.final_draft.exercises)
    assert resp.reply


def test_apply_recomputes_coverage_after_exclude():
    # Fix 3: the apply endpoint must NOT echo request.base_draft.coverage
    # unchanged — it must recompute "filled" from the post-command exercise
    # list so the mobile "цели X/Y" indicator isn't stale after an edit.
    profile = _profile()
    pool = []
    base = {"day_tag": "push", "day_name": "Push",
            "coverage": {"quads": {"target": 3, "filled": 3}, "chest": {"target": 3, "filled": 3}},
            "warnings": [],
            "exercises": [
                {"exercise_id": 1, "name": "Выпады", "target_sets": 3, "order_index": 0,
                 "superset_group_id": None, "fatigue_tier": 1,
                 "primary_muscle": "Квадрицепсы", "secondary_muscle": None},
                {"exercise_id": 2, "name": "Жим лёжа", "target_sets": 3, "order_index": 1,
                 "superset_group_id": None, "fatigue_tier": 1,
                 "primary_muscle": "Грудь", "secondary_muscle": None},
            ]}
    req = ApplyCommandsRequest(base_draft=base, command_log=[], new_comment="убери выпады", context={})
    with patch.object(plans_mod, "_load_commands_context",
                      AsyncMock(return_value=(profile, pool))):
        resp = asyncio.run(plans_mod.apply_commands(req, db=object(), current_user=SimpleNamespace(id=1)))
    coverage = resp.final_draft.coverage
    # the excluded exercise's muscle (quads) drops to 0 filled; target is unchanged
    assert coverage["quads"]["filled"] == 0
    assert coverage["quads"]["target"] == 3
    # the surviving exercise's muscle (chest) still shows its correct filled count
    assert coverage["chest"]["filled"] == 3
    assert coverage["chest"]["target"] == 3


def test_apply_clarify_on_ambiguous_replace():
    profile = _profile()
    pool = []
    base = {"day_tag": "push", "day_name": "Push", "coverage": {}, "warnings": [],
            "exercises": [
                {"exercise_id": 1, "name": "Жим штанги", "target_sets": 3, "order_index": 0,
                 "superset_group_id": None, "fatigue_tier": 1,
                 "primary_muscle": "Грудь", "secondary_muscle": None},
                {"exercise_id": 2, "name": "Жим гантелей", "target_sets": 3, "order_index": 1,
                 "superset_group_id": None, "fatigue_tier": 1,
                 "primary_muscle": "Грудь", "secondary_muscle": None},
            ]}
    req = ApplyCommandsRequest(base_draft=base, command_log=[],
                               new_comment="замени жим на изоляцию", context={})
    with patch.object(plans_mod, "_load_commands_context",
                      AsyncMock(return_value=(profile, pool))):
        resp = asyncio.run(plans_mod.apply_commands(req, db=object(), current_user=SimpleNamespace(id=1)))
    assert resp.clarify is not None
    assert resp.clarify.question
    # ambiguous comment is NOT applied: log stays empty, both exercises remain
    assert resp.parsed_commands == []
    ids = {e.exercise_id for e in resp.final_draft.exercises}
    assert ids == {1, 2}


def test_commands_apply_route_resolves_not_shadowed_by_plan_id():
    """Regression: POST /plans/commands/apply must resolve to apply_commands,
    not be captured by /plans/{plan_id}/apply (which would parse plan_id='commands').
    The other endpoint tests call the handler directly and bypass URL routing, so
    this guards the registration-order fix."""
    from starlette.routing import Match
    import api.routers.plans as plans_mod
    scope = {"type": "http", "method": "POST", "path": "/plans/commands/apply"}
    first = None
    for r in plans_mod.router.routes:
        mt, _ = r.matches(scope)
        if mt == Match.FULL:
            first = getattr(r, "name", None)
            break
    assert first == "apply_commands", f"route shadowed; first match was {first!r}"
