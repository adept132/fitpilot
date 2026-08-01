"""Orchestrates one generated training day: volume targets -> selected exercises
-> validated draft. Pure (no DB); callers load and pass data in."""
from dataclasses import dataclass, field
from typing import Optional

from api.services.exercise_selection_engine import (
    select_exercises, SelectionConfig, SelectionPolicy, SelectedExercise,
)
from api.services.muscle_keys import key_for_muscle
from api.services.validator import AntiSuicideValidator, PlanExerciseInput


@dataclass
class GeneratedDay:
    day_tag: str
    day_name: str
    exercises: list  # list[SelectedExercise]
    coverage: dict   # {en_key: {"target": int, "filled": int}}
    warnings: list = field(default_factory=list)


def build_day(day_tag: str, day_name: str, session_targets: dict, pool: list,
              allowed_equipment_keys: Optional[set], prehab_flags: list,
              experience_level: str, config: SelectionConfig,
              policy: SelectionPolicy = SelectionPolicy()) -> GeneratedDay:
    targets_int = {k: int(v) for k, v in session_targets.items()}
    exercises: list[SelectedExercise] = select_exercises(
        targets_int, pool, allowed_equipment_keys, prehab_flags, config, policy
    )

    # Coverage: direct sets per EN key.
    filled: dict[str, int] = {k: 0 for k in targets_int}
    for e in exercises:
        k = key_for_muscle(e.primary_muscle)
        if k in filled:
            filled[k] += e.sets
    coverage = {k: {"target": targets_int[k], "filled": filled.get(k, 0)} for k in targets_int}
    warnings = [f"Цель по '{k}' закрыта частично: {filled.get(k,0)}/{targets_int[k]} подходов"
                for k in targets_int if filled.get(k, 0) < targets_int[k]]

    # Validate (raises HTTPException on hard-cap / unsafe superset).
    AntiSuicideValidator.validate_workout_plan(
        experience_level,
        [PlanExerciseInput(exercise_id=e.exercise_id, fatigue_tier=e.fatigue_tier,
                           primary_muscle=e.primary_muscle, secondary_muscle=e.secondary_muscle,
                           target_sets=e.sets,
                           superset_group_id=e.superset_group_id) for e in exercises],
    )
    return GeneratedDay(day_tag=day_tag, day_name=day_name, exercises=exercises,
                        coverage=coverage, warnings=warnings)
