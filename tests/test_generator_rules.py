from types import SimpleNamespace

from api.routers.plans import _apply_saved_generator_rules, _rule_matches
from api.schemas.plan import GeneratedDayOut


def _day():
    return GeneratedDayOut(
        day_tag="upper #2",
        day_name="Upper",
        coverage={"chest": {"target": 6, "filled": 3}},
        exercises=[{
            "exercise_id": 1,
            "name": "Bench press",
            "target_sets": 3,
            "order_index": 0,
            "fatigue_tier": 1,
            "primary_muscle": "Грудь",
        }],
    )


def _pool():
    return [SimpleNamespace(
        id=1,
        name="Bench press",
        name_en=None,
        description=None,
        description_en=None,
        source="default",
    )]


def test_rule_scope_matching_normalizes_repeated_day_suffix():
    day_rule = {"enabled": True, "scope": "day", "blueprint_id": "bp-1", "day_tag": "upper"}
    split_rule = {"enabled": True, "scope": "split", "blueprint_id": "bp-1"}
    assert _rule_matches(day_rule, "bp-1", "upper #2")
    assert _rule_matches(split_rule, "bp-1", "lower")
    assert not _rule_matches(day_rule, "bp-2", "upper")
    assert not _rule_matches({**day_rule, "enabled": False}, "bp-1", "upper")


def test_saved_rule_replays_same_command_and_recomputes_coverage():
    profile = SimpleNamespace(experience_level="intermediate", settings={
        "generator_rules": [{
            "id": "rule-1",
            "enabled": True,
            "scope": "day",
            "blueprint_id": "bp-1",
            "day_tag": "upper",
            "command": {
                "type": "SET_ALL_SETS",
                "params": {"n": 4},
                "seed": 0,
                "confidence": 1.0,
                "summary": "4 подхода в каждом упражнении",
            },
        }],
    })
    result = _apply_saved_generator_rules(_day(), profile, "bp-1", _pool(), None, [])
    assert result.exercises[0].target_sets == 4
    assert result.coverage["chest"]["filled"] == 4
    assert any("Постоянное правило" in warning for warning in result.warnings)


def test_saved_rule_does_not_touch_unmatched_split():
    profile = SimpleNamespace(experience_level="intermediate", settings={
        "generator_rules": [{
            "id": "rule-1",
            "enabled": True,
            "scope": "split",
            "blueprint_id": "bp-other",
            "command": {"type": "SET_ALL_SETS", "params": {"n": 5}, "summary": "5 подходов"},
        }],
    })
    result = _apply_saved_generator_rules(_day(), profile, "bp-1", [], None, [])
    assert result.exercises[0].target_sets == 3


def test_rules_routes_are_not_shadowed_by_plan_id_routes():
    from starlette.routing import Match
    import api.routers.plans as plans_mod

    expected = {
        ("GET", "/plans/commands/rules"): "list_generator_rules",
        ("POST", "/plans/commands/rules"): "create_generator_rule",
        ("PATCH", "/plans/commands/rules/rule-1"): "update_generator_rule",
        ("DELETE", "/plans/commands/rules/rule-1"): "delete_generator_rule",
    }
    for (method, path), endpoint_name in expected.items():
        scope = {"type": "http", "method": method, "path": path}
        first = next((getattr(route, "name", None) for route in plans_mod.router.routes
                      if route.matches(scope)[0] == Match.FULL), None)
        assert first == endpoint_name, f"{method} {path} resolved to {first!r}"
