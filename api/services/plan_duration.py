"""Plan-time workout duration estimation and safe post-selection trimming."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Literal

from api.services import equipment as equip
from api.services.exercise_selection_engine import SelectedExercise
from api.services.muscle_keys import key_for_muscle
from api.services.resolvers import DayTacticalType, resolve_rep_range

TimerMode = Literal["smart", "fixed"]

STARTUP_SECONDS = 90
SUPERSET_SWITCH_SECONDS = 15
MIN_SETS_PER_EXERCISE = 2

REP_SECONDS = {1: 4.0, 2: 3.5, 3: 3.0}
SMART_REST_SECONDS = {1: 180, 2: 120, 3: 90}
EFFORT_REST_MODIFIER = {
    "easy": -30,
    "medium": 0,
    "hard": 30,
    "prefailure": 30,
    "failure": 60,
    "deload": -30,
}


@dataclass(frozen=True)
class DurationConfig:
    timer_mode: TimerMode = "smart"
    fixed_rest_seconds: int = 120
    day_effort: str = "medium"


@dataclass(frozen=True)
class DurationResult:
    exercises: list[SelectedExercise]
    estimated_seconds: int
    limit_met: bool
    reduced: bool


def _equipment_keys(exercise) -> set[str]:
    keys = set(equip.normalize_equipment_list(getattr(exercise, "equipment_needed", None) or []))
    return keys or {equip.BODYWEIGHT}


def preparation_seconds(exercise) -> int:
    keys = _equipment_keys(exercise)
    if equip.BARBELL in keys or equip.BENCH in keys:
        return 120
    if equip.FREE_MACHINE in keys or equip.SMITH in keys:
        return 90
    if keys & {equip.DUMBBELL, equip.KETTLEBELL}:
        return 60
    if equip.BLOCK_MACHINE in keys:
        return 45
    return 30


def rest_seconds(tier: int, config: DurationConfig) -> int:
    if config.timer_mode == "fixed":
        return max(0, config.fixed_rest_seconds)
    return max(30, SMART_REST_SECONDS.get(tier, 120) + EFFORT_REST_MODIFIER.get(config.day_effort, 0))


def set_execution_seconds(tier: int, day_effort: str) -> int:
    tactical = {
        "easy": DayTacticalType.easy,
        "medium": DayTacticalType.medium,
        "hard": DayTacticalType.hard,
        "prefailure": DayTacticalType.hard,
        "failure": DayTacticalType.hard,
        "deload": DayTacticalType.easy,
    }.get(day_effort, DayTacticalType.medium)
    _, rep_max = resolve_rep_range(tier, tactical)
    return math.ceil(10 + rep_max * REP_SECONDS.get(tier, 3.5))


def _blocks(exercises: list[SelectedExercise]) -> list[list[SelectedExercise]]:
    blocks: list[list[SelectedExercise]] = []
    for exercise in exercises:
        previous = blocks[-1] if blocks else []
        if (exercise.superset_group_id and previous
                and previous[0].superset_group_id == exercise.superset_group_id):
            previous.append(exercise)
        else:
            blocks.append([exercise])
    return blocks


def estimate_duration_seconds(
    exercises: list[SelectedExercise], pool_by_id: dict[int, object], config: DurationConfig,
) -> int:
    if not exercises:
        return 0
    total = STARTUP_SECONDS
    blocks = _blocks(exercises)
    previous_rest = 0
    for block_index, block in enumerate(blocks):
        setup = sum(preparation_seconds(pool_by_id[row.exercise_id]) for row in block)
        total += setup if block_index == 0 else max(previous_rest, setup)
        if len(block) == 1:
            row = block[0]
            execution = set_execution_seconds(row.fatigue_tier, config.day_effort)
            rest = rest_seconds(row.fatigue_tier, config)
            total += row.sets * execution + max(0, row.sets - 1) * rest
            previous_rest = rest
            continue

        rounds = max(row.sets for row in block)
        block_rest = max(rest_seconds(row.fatigue_tier, config) for row in block)
        for round_index in range(rounds):
            active = [row for row in block if row.sets > round_index]
            total += sum(set_execution_seconds(row.fatigue_tier, config.day_effort) for row in active)
            total += max(0, len(active) - 1) * SUPERSET_SWITCH_SECONDS
            if round_index < rounds - 1:
                total += block_rest
        previous_rest = block_rest
    return int(total)


def _copy_with_sets(exercise: SelectedExercise, sets: int, group_id=None) -> SelectedExercise:
    return SelectedExercise(
        exercise_id=exercise.exercise_id, name=exercise.name, sets=sets,
        order_index=exercise.order_index, superset_group_id=group_id if group_id is not None else exercise.superset_group_id,
        fatigue_tier=exercise.fatigue_tier, primary_muscle=exercise.primary_muscle,
        secondary_muscle=exercise.secondary_muscle,
    )


def _normalize_groups(exercises: list[SelectedExercise]) -> list[SelectedExercise]:
    counts: dict[str, int] = {}
    for row in exercises:
        if row.superset_group_id:
            counts[row.superset_group_id] = counts.get(row.superset_group_id, 0) + 1
    result = []
    for index, row in enumerate(exercises):
        group_size = counts.get(row.superset_group_id, 0) if row.superset_group_id else 0
        group = row.superset_group_id if group_size in (2, 3) else None
        copied = _copy_with_sets(row, row.sets, group_id=group)
        copied.order_index = index
        result.append(copied)
    return result


def fit_to_duration(
    exercises: list[SelectedExercise], pool_by_id: dict[int, object], duration_minutes: int | None,
    config: DurationConfig, accent_muscles: Iterable[str] = (), favorite_ids: set[int] | None = None,
) -> DurationResult:
    current = [_copy_with_sets(row, row.sets) for row in exercises]
    estimate = estimate_duration_seconds(current, pool_by_id, config)
    if not duration_minutes:
        return DurationResult(current, estimate, True, False)
    limit = duration_minutes * 60
    if estimate <= limit:
        return DurationResult(current, estimate, True, False)

    accents = set(accent_muscles)
    favorites = favorite_ids or set()
    reduced = False
    while estimate > limit:
        reducible = [row for row in current if row.sets > MIN_SETS_PER_EXERCISE]
        if reducible:
            # Preserve accents and favorites longest; trim isolation before heavy compounds.
            victim = min(reducible, key=lambda row: (
                key_for_muscle(row.primary_muscle) in accents,
                row.exercise_id in favorites,
                -row.fatigue_tier,
                -row.sets,
                -row.order_index,
            ))
            index = current.index(victim)
            current[index] = _copy_with_sets(victim, victim.sets - 1)
        else:
            muscle_counts: dict[str | None, int] = {}
            for row in current:
                key = key_for_muscle(row.primary_muscle)
                muscle_counts[key] = muscle_counts.get(key, 0) + 1
            action_counts: dict[object, int] = {}
            for row in current:
                action = getattr(pool_by_id[row.exercise_id], "action", None)
                action_counts[action] = action_counts.get(action, 0) + 1
            removable = [
                row for row in current
                if muscle_counts.get(key_for_muscle(row.primary_muscle), 0) > 1
                or (
                    row.fatigue_tier == 3
                    and key_for_muscle(row.primary_muscle) not in accents
                    and row.exercise_id not in favorites
                    # Never remove the sole representative of a structural
                    # movement pattern just to satisfy a cosmetic time limit.
                    and action_counts.get(getattr(pool_by_id[row.exercise_id], "action", None), 0) > 1
                )
            ]
            if not removable:
                break
            victim = min(removable, key=lambda row: (
                key_for_muscle(row.primary_muscle) in accents,
                row.exercise_id in favorites,
                -row.fatigue_tier,
                row.sets,
                -row.order_index,
            ))
            current.remove(victim)
            current = _normalize_groups(current)
        reduced = True
        estimate = estimate_duration_seconds(current, pool_by_id, config)

    return DurationResult(_normalize_groups(current), estimate, estimate <= limit, reduced)
