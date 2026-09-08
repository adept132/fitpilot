from api.schemas.supersets import SupersetFlowExerciseItem


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
