from datetime import date
from types import SimpleNamespace

from api.schemas.plan import GeneratedDayOut, GeneratedExerciseOut
from api.services.exercise_pattern_tags import ExerciseAction
from api.services.plan_generation_insights import (
    compare_generated_day, explain_day, missing_requested_day_issue,
    missing_volume_targets_issue,
)


def _generated(exercise_id=1, sets=3):
    return GeneratedDayOut(
        day_tag="push", day_name="Push", warnings=[],
        coverage={"chest": {"target": 4, "filled": sets}},
        exercises=[GeneratedExerciseOut(
            exercise_id=exercise_id, name=f"Exercise {exercise_id}",
            target_sets=sets, order_index=1, fatigue_tier=2,
            primary_muscle="Грудь",
        )],
    )


def _plan(exercise_id=1, sets=3):
    exercise = SimpleNamespace(id=exercise_id, name=f"Exercise {exercise_id}")
    row = SimpleNamespace(
        exercise_id=exercise_id, exercise=exercise, target_sets=sets,
        order_index=1, superset_group_id=None,
        override_reps=None, override_rir=None,
    )
    return SimpleNamespace(id=10, name="Old push", exercises=[row])


def _pool(equipment=None):
    return [SimpleNamespace(
        id=1, name="Exercise 1", action=ExerciseAction.push, fatigue_tier=2,
        equipment_needed=equipment or ["dumbbell"], main_muscle_group="Грудь",
    )]


def test_comparison_reports_unchanged_plan_and_affected_dates():
    result = compare_generated_day(
        _generated(), [_plan()], {10: "calendar_and_split"},
        [date(2026, 8, 20), date(2026, 8, 22)],
    )

    assert result.status == "unchanged"
    assert result.affected_calendar_days == 2
    assert result.previous_plans[0].source == "calendar_and_split"


def test_comparison_reports_added_removed_and_modified_exercises():
    added = compare_generated_day(_generated(2), [_plan(1)], {10: "split"}, [])
    modified = compare_generated_day(_generated(1, 4), [_plan(1, 3)], {10: "split"}, [])

    assert added.status == "changed"
    assert [row.exercise_id for row in added.added_exercises] == [2]
    assert [row.exercise_id for row in added.removed_exercises] == [1]
    assert [row.exercise_id for row in modified.modified_exercises] == [1]


def test_comparison_localizes_missing_exercise_name_fallback():
    old = _plan(1)
    old.exercises[0].exercise = None

    result = compare_generated_day(_generated(2), [old], {10: "split"}, [], language="en")

    assert result.removed_exercises[0].name == "Exercise #1"
    assert result.removed_exercises[0].localized_names == {"en": "Exercise #1"}


def test_issues_explain_duration_and_inapplicable_accent():
    issues = explain_day(
        _generated(sets=4), {"chest": 8}, _pool(), None, [], 30, "lats",
    )

    assert {issue.code for issue in issues} >= {
        "duration_reduced_volume", "accent_not_in_day",
    }
    assert next(i for i in issues if i.code == "duration_reduced_volume").action.type == "change_duration"


def test_partial_coverage_distinguishes_context_filter_from_empty_library():
    day = _generated(sets=0)
    context_issue = explain_day(
        day, {"chest": 4}, _pool(["barbell"]), {"bodyweight"}, [], None, None,
    )
    library_issue = explain_day(
        day, {"chest": 4}, [], None, [], None, None,
    )

    assert next(i for i in context_issue if i.code == "muscle_target_partially_covered").action.type == "review_limitations"
    assert next(i for i in library_issue if i.code == "muscle_target_partially_covered").action.type == "edit_day"


def test_structured_blockers_offer_direct_recovery_actions():
    missing_day = missing_requested_day_issue("Legs")
    missing_volume = missing_volume_targets_issue("Push")

    assert missing_day.severity == "blocking"
    assert missing_day.action.type == "edit_split"
    assert missing_volume.action.type == "edit_volume"
