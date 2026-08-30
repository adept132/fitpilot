"""Explain generated plans without coupling the explanation to presentation text."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable

from api.i18n import SupportedLanguage, tr
from api.schemas.plan import (
    ComparedExercise,
    GeneratedDayComparison,
    GeneratedDayOut,
    GenerationIssue,
    GenerationIssueAction,
    PreviousPlanSummary,
)
from api.services.exercise_selection_engine import filter_pool
from api.services.exercise_localization import localized_descriptions, localized_names
from api.services.muscle_keys import key_for_muscle


def _exercise_rows(plan) -> list:
    return sorted(list(getattr(plan, "exercises", []) or []), key=lambda row: row.order_index)


def _exercise_name(row) -> str:
    exercise = getattr(row, "exercise", None)
    return getattr(exercise, "name", None) or f"Упражнение #{row.exercise_id}"


def _compared_exercise(row) -> ComparedExercise:
    exercise = getattr(row, "exercise", None)
    name = (
        getattr(exercise, "name", None)
        or getattr(row, "name", None)
        or _exercise_name(row)
    )
    names = (
        localized_names(exercise)
        if exercise is not None
        else dict(getattr(row, "localized_names", {}) or {})
    )
    descriptions = (
        localized_descriptions(exercise)
        if exercise is not None
        else dict(getattr(row, "localized_descriptions", {}) or {})
    )
    if not names and name:
        names = {"ru": name}
    return ComparedExercise(
        exercise_id=row.exercise_id,
        name=name,
        localized_names=names,
        localized_descriptions=descriptions,
    )


def _superset_pattern(rows: list) -> list[int | None]:
    groups: dict[str, int] = {}
    result: list[int | None] = []
    for row in rows:
        raw = getattr(row, "superset_group_id", None)
        if raw is None:
            result.append(None)
            continue
        key = str(raw)
        if key not in groups:
            groups[key] = len(groups) + 1
        result.append(groups[key])
    return result


def _signature(rows: list) -> list[tuple]:
    groups = _superset_pattern(rows)
    return [
        (
            row.exercise_id,
            row.target_sets,
            getattr(row, "override_reps", None),
            getattr(row, "override_rir", None),
            groups[index],
        )
        for index, row in enumerate(rows)
    ]


def compare_generated_day(
    day: GeneratedDayOut,
    previous_plans: list,
    sources: dict[int, str],
    affected_dates: Iterable[date],
) -> GeneratedDayComparison:
    generated_rows = list(day.exercises)
    previous_rows = _exercise_rows(previous_plans[0]) if previous_plans else []
    generated_signature = _signature(generated_rows)
    previous_signature = _signature(previous_rows)

    old_by_id = {row.exercise_id: row for row in previous_rows}
    new_by_id = {row.exercise_id: row for row in generated_rows}
    added = [
        _compared_exercise(new_by_id[key])
        for key in new_by_id.keys() - old_by_id.keys()
    ]
    removed = [
        _compared_exercise(old_by_id[key])
        for key in old_by_id.keys() - new_by_id.keys()
    ]
    modified = []
    for key in new_by_id.keys() & old_by_id.keys():
        old = old_by_id[key]
        new = new_by_id[key]
        if (
            old.target_sets != new.target_sets
            or getattr(old, "override_reps", None) != new.override_reps
            or getattr(old, "override_rir", None) != new.override_rir
        ):
            modified.append(_compared_exercise(new))

    dates = sorted(set(affected_dates))
    status = "new" if not previous_plans else (
        "unchanged"
        if all(_signature(_exercise_rows(plan)) == generated_signature for plan in previous_plans)
        else "changed"
    )
    return GeneratedDayComparison(
        day_tag=day.day_tag,
        day_name=day.day_name,
        status=status,
        previous_plans=[
            PreviousPlanSummary(
                plan_id=plan.id,
                name=plan.name,
                source=sources.get(plan.id, "split"),
            )
            for plan in previous_plans
        ],
        affected_calendar_days=len(dates),
        affected_dates=dates[:5],
        previous_exercise_count=len(previous_rows),
        generated_exercise_count=len(generated_rows),
        previous_sets=sum(row.target_sets for row in previous_rows),
        generated_sets=sum(row.target_sets for row in generated_rows),
        added_exercises=sorted(added, key=lambda row: row.exercise_id),
        removed_exercises=sorted(removed, key=lambda row: row.exercise_id),
        modified_exercises=sorted(modified, key=lambda row: row.exercise_id),
    )


def explain_day(
    day: GeneratedDayOut,
    original_targets: dict[str, int],
    pool: list,
    allowed_equipment: set | None,
    prehab_flags: list[str],
    duration_minutes: int | None,
    accent_muscles: list[str] | tuple[str, ...] | None,
    disliked_exercise_ids: set[int] | None = None,
    language: SupportedLanguage = "ru",
) -> list[GenerationIssue]:
    issues: list[GenerationIssue] = []
    for accent_muscle in dict.fromkeys(accent_muscles or []):
        if accent_muscle in original_targets:
            continue
        issues.append(GenerationIssue(
            code="accent_not_in_day",
            severity="warning",
            day_tag=day.day_tag,
            muscle=accent_muscle,
            params={"muscle": accent_muscle},
            title="",
            reason="",
            action=GenerationIssueAction(
                type="change_accent", label="",
            ),
        ))

    original_total = sum(original_targets.values())
    generated_sets = sum(exercise.target_sets for exercise in day.exercises)
    if duration_minutes and generated_sets < original_total:
        issues.append(GenerationIssue(
            code="duration_reduced_volume",
            severity="info",
            day_tag=day.day_tag,
            title="",
            reason="",
            params={
                "duration_minutes": duration_minutes,
                "original_sets": original_total,
                "generated_sets": generated_sets,
            },
            action=GenerationIssueAction(
                type="change_duration", label="",
                params={"current_minutes": duration_minutes},
            ),
        ))
    if duration_minutes and not day.duration_limit_met:
        minimum_minutes = max(1, round(day.estimated_duration_seconds / 60))
        issues.append(GenerationIssue(
            code="duration_limit_unreachable",
            severity="blocking",
            day_tag=day.day_tag,
            title="",
            reason="",
            params={"minimum_minutes": minimum_minutes},
            action=GenerationIssueAction(
                type="change_duration", label="",
                params={"minimum_minutes": minimum_minutes},
            ),
        ))

    eligible_before_preferences = filter_pool(pool, allowed_equipment, prehab_flags)
    disliked_exercise_ids = disliked_exercise_ids or set()
    eligible = [ex for ex in eligible_before_preferences if ex.id not in disliked_exercise_ids]
    eligible_muscles = {key_for_muscle(ex.main_muscle_group) for ex in eligible}
    eligible_before_preference_muscles = {
        key_for_muscle(ex.main_muscle_group) for ex in eligible_before_preferences
    }
    all_muscles = {key_for_muscle(ex.main_muscle_group) for ex in pool}
    for muscle, coverage in day.coverage.items():
        target = coverage.get("target", 0)
        filled = coverage.get("filled", 0)
        if filled >= target:
            continue
        if muscle in eligible_before_preference_muscles and muscle not in eligible_muscles:
            issues.append(GenerationIssue(
                code="muscle_target_blocked_by_preferences",
                severity="warning" if filled else "blocking",
                day_tag=day.day_tag,
                muscle=muscle,
                title="",
                reason="",
                params={"filled_sets": filled, "target_sets": target, "muscle": muscle},
                action=GenerationIssueAction(
                    type="review_preferences",
                    label="",
                    params={"day_tag": day.day_tag, "muscle": muscle},
                ),
            ))
            continue
        blocked_by_context = muscle in all_muscles and muscle not in eligible_muscles
        action_type = "review_limitations" if blocked_by_context else "edit_day"
        issues.append(GenerationIssue(
            code="muscle_target_partially_covered",
            severity="warning" if filled else "blocking",
            day_tag=day.day_tag,
            muscle=muscle,
            title="",
            reason="",
            body_key=(
                "generation.muscle_target_partially_covered.context_body"
                if blocked_by_context
                else "generation.muscle_target_partially_covered.library_body"
            ),
            params={"filled_sets": filled, "target_sets": target, "muscle": muscle},
            action=GenerationIssueAction(
                type=action_type,
                label="",
                label_key=(
                    "generation.muscle_target_partially_covered.context_action"
                    if blocked_by_context
                    else "generation.muscle_target_partially_covered.library_action"
                ),
                params={"day_tag": day.day_tag, "muscle": muscle},
            ),
        ))

    if not day.exercises:
        issues.append(GenerationIssue(
            code="empty_training_day",
            severity="blocking",
            day_tag=day.day_tag,
            title="",
            reason="",
            params={"day_tag": day.day_tag},
            action=GenerationIssueAction(
                type="review_limitations", label="",
                params={"day_tag": day.day_tag},
            ),
        ))
    return [_localized_issue(issue, language) for issue in issues]


def _localized_issue(
    issue: GenerationIssue, language: SupportedLanguage
) -> GenerationIssue:
    title_key = issue.title_key or f"generation.{issue.code}.title"
    body_key = issue.body_key or f"generation.{issue.code}.body"
    action_key = issue.action.label_key or f"generation.{issue.code}.action"
    return issue.model_copy(
        update={
            "title_key": title_key,
            "body_key": body_key,
            "title": tr(language, title_key, **issue.params),
            "reason": tr(language, body_key, **issue.params),
            "action": issue.action.model_copy(
                update={
                    "label_key": action_key,
                    "label": tr(language, action_key, **issue.action.params),
                }
            ),
        }
    )


def missing_requested_day_issue(
    day_name: str, language: SupportedLanguage = "ru"
) -> GenerationIssue:
    return _localized_issue(GenerationIssue(
        code="requested_day_not_found",
        severity="blocking",
        day_tag=day_name,
        title="",
        reason="",
        params={"day_name": day_name},
        action=GenerationIssueAction(
            type="edit_split", label="",
            params={"day_name": day_name},
        ),
    ), language)


def missing_volume_targets_issue(
    day_tag: str, language: SupportedLanguage = "ru"
) -> GenerationIssue:
    return _localized_issue(GenerationIssue(
        code="missing_volume_targets",
        severity="blocking",
        day_tag=day_tag,
        title="",
        reason="",
        params={"day_tag": day_tag},
        action=GenerationIssueAction(
            type="edit_volume", label="",
            params={"day_tag": day_tag},
        ),
    ), language)
