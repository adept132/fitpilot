"""Принятая правка предписания попадает в реальную тренировку.

Без этого теста решение сработало бы в интерфейсе и не сработало бы в
железе — та же ловушка, что resolve_phase_effort_tier в P0-08.
"""
import pytest
from sqlalchemy import select

from api.services.models import (
    UserCalendarDay,
    WorkoutPlanExercise,
    WorkoutSessionExercise,
)
from api.services.volume.repository import utc_today

pytestmark = pytest.mark.asyncio


async def test_adjustment_changes_target_sets_of_created_session(
    client, auth_headers, db, test_user, seeded_plan, seeded_history
):
    day = UserCalendarDay(
        app_user_id=test_user.id,
        target_date=utc_today(),
        day_tag="push",
        micro_tag="medium",
        meso_tag="medium",
        microcycle_day_number=1,
        plan_id=seeded_plan.id,
        is_rest_day=False,
        is_blackout=False,
        status="planned",
        volume_adjustments=[
            {"exercise_id": seeded_history.id, "delta_sets": 2, "proposal_id": 1}
        ],
    )
    db.add(day)
    await db.commit()
    await db.refresh(day)

    baseline = (await db.execute(
        select(WorkoutPlanExercise.target_sets).where(
            WorkoutPlanExercise.plan_id == seeded_plan.id,
            WorkoutPlanExercise.exercise_id == seeded_history.id,
        )
    )).scalar_one()

    resp = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={
            "source": "free",
            "plan_id": seeded_plan.id,
            "calendar_day_id": day.id,
        },
    )
    assert resp.status_code == 200
    workout_id = resp.json()["id"]

    created = (await db.execute(
        select(WorkoutSessionExercise).where(
            WorkoutSessionExercise.workout_session_id == workout_id,
            WorkoutSessionExercise.exercise_id == seeded_history.id,
        )
    )).scalar_one()

    assert created.target_sets == baseline + 2


async def test_session_without_calendar_day_ignores_adjustments(
    client, auth_headers, db, test_user, seeded_plan, seeded_history
):
    """Пинит: наличие правки в БД само по себе не значит, что она применится.

    Прежняя форма теста создавала запрос без calendar_day_id, но НЕ создавала
    вообще никакого UserCalendarDay с volume_adjustments — в базе не было
    правки, которую можно было бы ошибочно наложить, поэтому «правки не
    применились» было тривиально верно и без починки apply_adjustments.
    Тест был зелёным и до, и после того как apply_adjustments подключили к
    start_workout (см. RED-лог задачи 7), то есть не проверял ничего.

    Здесь день с непустым volume_adjustments (+5 подходов на seeded_history)
    СУЩЕСТВУЕТ в базе — но запрос идёт с plan_id и БЕЗ calendar_day_id, то
    есть на этот день не ссылается. Правка в базе есть, но запрос на неё не
    ссылается — значит применяться она не имеет права: сохранённый
    target_sets обязан остаться ровно шаблонным (baseline), без +5.
    """
    day = UserCalendarDay(
        app_user_id=test_user.id,
        target_date=utc_today(),
        day_tag="push",
        micro_tag="medium",
        meso_tag="medium",
        microcycle_day_number=1,
        plan_id=seeded_plan.id,
        is_rest_day=False,
        is_blackout=False,
        status="planned",
        volume_adjustments=[
            {"exercise_id": seeded_history.id, "delta_sets": 5, "proposal_id": 2}
        ],
    )
    db.add(day)
    await db.commit()
    await db.refresh(day)

    baseline = (await db.execute(
        select(WorkoutPlanExercise.target_sets).where(
            WorkoutPlanExercise.plan_id == seeded_plan.id,
            WorkoutPlanExercise.exercise_id == seeded_history.id,
        )
    )).scalar_one()

    resp = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={"source": "free", "plan_id": seeded_plan.id},
    )
    assert resp.status_code == 200
    workout_id = resp.json()["id"]

    created = (await db.execute(
        select(WorkoutSessionExercise).where(
            WorkoutSessionExercise.workout_session_id == workout_id,
            WorkoutSessionExercise.exercise_id == seeded_history.id,
        )
    )).scalar_one()

    assert created.target_sets == baseline
