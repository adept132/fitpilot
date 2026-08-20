from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.schemas.plan import PlanExerciseCreate
from api.services.validator import AntiSuicideValidator, PlanExerciseInput


def _schema_row(**overrides):
    values = dict(
        exercise_id=1, order_index=1, target_sets=3, fatigue_tier=2,
        primary_muscle="chest", secondary_muscle="triceps",
        override_reps="6-8", override_rir=2,
    )
    values.update(overrides)
    return PlanExerciseCreate(**values)


def _validator_row(exercise_id: int, group_id=None):
    return PlanExerciseInput(
        exercise_id=exercise_id, fatigue_tier=2, primary_muscle=f"muscle-{exercise_id}",
        secondary_muscle=None, target_sets=1, superset_group_id=group_id,
    )


def test_manual_plan_contract_preserves_prescription():
    exercise = _schema_row()
    assert exercise.override_reps == "6-8"
    assert exercise.override_rir == 2


@pytest.mark.parametrize("reps", ["many", "8-10-12", ""])
def test_manual_plan_contract_rejects_invalid_reps(reps):
    with pytest.raises(ValidationError):
        _schema_row(override_reps=reps)


def test_singleton_superset_is_rejected():
    with pytest.raises(HTTPException, match="как минимум два"):
        AntiSuicideValidator.validate_workout_plan("advanced", [_validator_row(1, uuid4())])


def test_superset_larger_than_three_is_rejected():
    group_id = uuid4()
    with pytest.raises(HTTPException, match="не больше трёх"):
        AntiSuicideValidator.validate_workout_plan(
            "advanced", [_validator_row(index, group_id) for index in range(1, 5)],
        )
