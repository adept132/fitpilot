from types import SimpleNamespace

import pytest

from api.schemas.supersets import SupersetFlowExerciseItem
from api.services.workout_superset_service import WorkoutSupersetService


def test_superset_flow_exposes_timer_and_stepper_metadata():
    item = SupersetFlowExerciseItem(
        session_exercise_id=1,
        order_index=0,
        exercise_id=10,
        exercise_name="Cable row",
        sets=[],
        is_current_round_completed=False,
        fatigue_tier=3,
        equipment_needed=["cable"],
    )

    payload = item.model_dump()
    assert payload["fatigue_tier"] == 3
    assert payload["equipment_needed"] == ["cable"]


def _schema_item():
    return SupersetFlowExerciseItem(
        session_exercise_id=1,
        order_index=0,
        exercise_id=10,
        exercise_name="Cable row",
        sets=[],
        is_current_round_completed=False,
    )


def test_superset_flow_equipment_default_is_independent():
    first = _schema_item()
    second = _schema_item()

    first.equipment_needed.append("cable")

    assert second.equipment_needed == []


class _FakeResult:
    def __init__(self, exercises):
        self._exercises = exercises

    def scalars(self):
        return self

    def all(self):
        return self._exercises


class _FakeSession:
    def __init__(self, exercises):
        self._exercises = exercises

    async def execute(self, _statement):
        return _FakeResult(self._exercises)


def _session_exercise(*, session_id, exercise_id, exercise):
    return SimpleNamespace(
        id=session_id,
        order_index=session_id,
        exercise_id=exercise_id,
        exercise=exercise,
        sets=[],
        superset_group="group-a",
        recommended_rir=None,
        recommended_rep_min=None,
        recommended_rep_max=None,
        target_sets=None,
        workout_session_id=99,
    )


@pytest.mark.asyncio
async def test_superset_flow_metadata_falls_back_when_relation_or_equipment_is_absent(
    monkeypatch,
):
    exercises = [
        _session_exercise(session_id=1, exercise_id=10, exercise=None),
        _session_exercise(
            session_id=2,
            exercise_id=20,
            exercise=SimpleNamespace(
                name="Cable row",
                fatigue_tier=2,
                equipment_needed=None,
            ),
        ),
    ]

    async def no_last_performance_sets(**_kwargs):
        return []

    monkeypatch.setattr(
        WorkoutSupersetService,
        "_get_last_performance_sets",
        staticmethod(no_last_performance_sets),
    )

    payload = await WorkoutSupersetService.get_superset_flow(
        session=_FakeSession(exercises),
        app_user_id=7,
        superset_group="group-a",
    )

    first, second = payload["exercises"]
    assert first["fatigue_tier"] is None
    assert first["equipment_needed"] == []
    assert second["fatigue_tier"] == 2
    assert second["equipment_needed"] == []
