"""Orchestrates one generated training day: volume targets -> selected exercises
-> validated draft. Pure (no DB); callers load and pass data in."""
from dataclasses import dataclass, field
from typing import Optional

from api.services.exercise_selection_engine import (
    select_exercises, SelectionConfig, SelectionPolicy, SelectedExercise,
    configured_targets,
)
from api.services.muscle_keys import key_for_muscle
from api.services.validator import AntiSuicideValidator, PlanExerciseInput
from api.services.plan_duration import DurationConfig, fit_to_duration


@dataclass
class GeneratedDay:
    day_tag: str
    day_name: str
    exercises: list  # list[SelectedExercise]
    coverage: dict   # {en_key: {"target": int, "filled": int}}
    warnings: list = field(default_factory=list)
    estimated_duration_seconds: int = 0
    duration_limit_met: bool = True


def build_day(day_tag: str, day_name: str, session_targets: dict, pool: list,
              allowed_equipment_keys: Optional[set], prehab_flags: list,
              experience_level: str, config: SelectionConfig,
              policy: SelectionPolicy = SelectionPolicy(),
              timer_mode: str = "smart", fixed_rest_seconds: int = 120,
              day_effort: str = "medium") -> GeneratedDay:
    configured = configured_targets(session_targets, config)
    set_cap = AntiSuicideValidator.workout_set_cap(experience_level)
    capped_targets = {
        muscle: min(target, set_cap)
        for muscle, target in configured.items()
    }
    cap_warnings = [
        f"Объём по '{muscle}' урезан с {target} до безопасного лимита {set_cap} подходов."
        for muscle, target in configured.items()
        if target > set_cap
    ]
    targets_int = capped_targets
    exercises: list[SelectedExercise] = select_exercises(
        targets_int, pool, allowed_equipment_keys, prehab_flags,
        SelectionConfig(
            use_supersets=config.use_supersets,
            max_superset_size=config.max_superset_size,
            seed=config.seed,
            favorite_exercise_ids=config.favorite_exercise_ids,
            disliked_exercise_ids=config.disliked_exercise_ids,
        ), policy
    )
    selected_filled: dict[str, int] = {k: 0 for k in targets_int}
    for exercise in exercises:
        key = key_for_muscle(exercise.primary_muscle)
        if key in selected_filled:
            selected_filled[key] += exercise.sets
    duration_result = fit_to_duration(
        exercises,
        {exercise.id: exercise for exercise in pool},
        config.duration_minutes,
        DurationConfig(
            timer_mode=timer_mode,
            fixed_rest_seconds=fixed_rest_seconds,
            day_effort=day_effort,
        ),
        accent_muscles=config.accent_muscles or (
            [config.accent_muscle] if config.accent_muscle else []
        ),
        favorite_ids=config.favorite_exercise_ids,
    )
    exercises = duration_result.exercises

    # Coverage: direct sets per EN key.
    filled: dict[str, int] = {k: 0 for k in targets_int}
    for e in exercises:
        k = key_for_muscle(e.primary_muscle)
        if k in filled:
            filled[k] += e.sets
    coverage = {
        k: {
            # A time-limit reduction creates a new attainable target. Preserve
            # genuine pre-existing selection gaps, but do not present deliberate
            # trimming as a missing-exercise error.
            "target": min(targets_int[k], filled.get(k, 0))
            if filled.get(k, 0) < selected_filled.get(k, 0)
            else targets_int[k],
            "filled": filled.get(k, 0),
        }
        for k in targets_int
    }
    warnings = cap_warnings + [f"Цель по '{k}' закрыта частично: {filled.get(k,0)}/{coverage[k]['target']} подходов"
                for k in targets_int if filled.get(k, 0) < coverage[k]["target"]]

    # Validate (raises HTTPException on hard-cap / unsafe superset).
    AntiSuicideValidator.validate_workout_plan(
        experience_level,
        [PlanExerciseInput(exercise_id=e.exercise_id, fatigue_tier=e.fatigue_tier,
                           primary_muscle=e.primary_muscle, secondary_muscle=e.secondary_muscle,
                           target_sets=e.sets,
                           superset_group_id=e.superset_group_id) for e in exercises],
    )
    if config.duration_minutes and not duration_result.limit_met:
        warnings.append(
            f"Минимально допустимый вариант занимает около "
            f"{round(duration_result.estimated_seconds / 60)} мин"
        )
    return GeneratedDay(day_tag=day_tag, day_name=day_name, exercises=exercises,
                        coverage=coverage, warnings=warnings,
                        estimated_duration_seconds=duration_result.estimated_seconds,
                        duration_limit_met=duration_result.limit_met)
