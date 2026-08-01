from api.schemas.plan import (
    GeneratePlanRequest, GeneratedDayOut, GeneratedExerciseOut, ConfirmPlanRequest,
)


def test_generate_request_defaults():
    req = GeneratePlanRequest()
    assert req.blueprint_id is None
    assert req.config.use_supersets is False


def test_day_out_roundtrip():
    day = GeneratedDayOut(day_tag="push", day_name="Push", exercises=[
        GeneratedExerciseOut(exercise_id=1, name="Жим", target_sets=3, order_index=0,
                             superset_group_id=None, fatigue_tier=1,
                             primary_muscle="Грудь", secondary_muscle="Трицепс")
    ], coverage={"chest": {"target": 6, "filled": 6}}, warnings=[])
    req = ConfirmPlanRequest(days=[day])
    assert req.days[0].exercises[0].exercise_id == 1
