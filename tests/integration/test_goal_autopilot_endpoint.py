"""Контекст экрана автопилота (P0-12, Задача 11)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from api.services.goal.service import build_context
from api.services.models import (
    PeriodizationProposal,
    UserCalendarDay,
    UserExerciseProgressionState,
    UserGoal,
)
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


_NO_DEADLINE_GIVEN = object()


async def _goal(
    user_id: int, exercise_id: int, primary: bool = True,
    deadline=_NO_DEADLINE_GIVEN, goal_type: str = "strength",
) -> int:
    if deadline is _NO_DEADLINE_GIVEN:
        deadline = date.today() + timedelta(days=60)
    async with SessionLocal() as db:
        goal = UserGoal(
            app_user_id=user_id, goal_type=goal_type, target_value=120.0,
            exercise_id=exercise_id, target_reps=3,
            deadline=deadline,
            is_primary=primary,
        )
        db.add(goal)
        await db.commit()
        await db.refresh(goal)
        return goal.id


async def _set_working_e1rm(user_id: int, exercise_id: int, working_e1rm: float) -> None:
    async with SessionLocal() as db:
        db.add(UserExerciseProgressionState(
            app_user_id=user_id, exercise_id=exercise_id, working_e1rm=working_e1rm,
        ))
        await db.commit()


async def test_returns_shape_even_without_history(client, auth_headers, test_user, fresh_exercise):
    goal_id = await _goal(test_user.id, fresh_exercise.id)
    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"eta", "rates", "milestones", "plan_ahead", "proposal",
                         "last_applied", "available", "unavailable_reason"}
    assert body["available"] is False
    assert body["unavailable_reason"]


async def test_foreign_goal_is_not_found(client, auth_headers, test_user, fresh_exercise):
    r = await client.get("/goals/999999/autopilot", headers=auth_headers)
    assert r.status_code == 404


async def test_not_strength_goal_gives_specific_reason(client, auth_headers, test_user):
    goal_id = await _goal(test_user.id, exercise_id=None, primary=False, goal_type="body_fat")
    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert "питани" in body["unavailable_reason"].lower()


async def test_no_deadline_gives_specific_reason(client, auth_headers, test_user, fresh_exercise):
    goal_id = await _goal(test_user.id, fresh_exercise.id, primary=False, deadline=None)
    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert "срок" in body["unavailable_reason"].lower()


async def test_no_active_block_gives_specific_reason(
    client, auth_headers, test_user, seeded_history
):
    """evaluate() успешен (рабочий e1RM есть), но активного блока нет —
    молчание с отдельной причиной, а не общее "нет истории"."""
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_id = await _goal(test_user.id, seeded_history.id)
    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert "блок" in body["unavailable_reason"].lower()


async def test_returns_full_shape_when_available(
    client, auth_headers, test_user, seeded_history, active_block
):
    """Обязательный случай сверх пары «доступно/нет» из брифа (см. Global
    Constraints задачи): тест формы обязан покрывать и путь, где автопилот
    реально доступен, а не только те, что гасят его на раннем return."""
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_id = await _goal(test_user.id, seeded_history.id)

    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["available"] is True
    assert body["unavailable_reason"] is None
    assert isinstance(body["rates"]["required"], (int, float))
    assert isinstance(body["rates"]["plan"], (int, float))
    assert isinstance(body["rates"]["ceiling"], (int, float))
    assert body["rates"]["ceiling"] > 0
    assert isinstance(body["milestones"], list)
    assert isinstance(body["plan_ahead"]["target_lift_sessions"], int)
    # Без сгенерированного плана в календаре — 0 будущих сессий лифта, но
    # поле обязано быть реально посчитано, а не заглушкой.
    assert body["plan_ahead"]["target_lift_sessions"] == 0
    assert body["proposal"] is None
    assert body["last_applied"] is None


# --- can_undo обязан совпадать с воротами undo_goal_decision (Задача 10) ---
#
# _undo_blocked_reason смотрит ТОЛЬКО на дни снимка с touched=True — то же
# множество, что и реальный откат (см. её докстринг). Наивная реализация по
# ВСЕМ дням снимка запретила бы отмену там, где /undo её реально разрешает.

async def test_can_undo_true_when_only_untouched_day_has_fact(
    test_user, seeded_history, active_block
):
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_id = await _goal(test_user.id, seeded_history.id)

    touched_date = date.today() + timedelta(days=1)
    untouched_date = date.today() + timedelta(days=2)
    async with SessionLocal() as db:
        # Нетронутый регенерацией день несёт факт — он не входит в touched
        # и потому не имеет права блокировать откат.
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=untouched_date,
            block_id=active_block.id, day_tag="pull", micro_tag="medium",
            meso_tag="medium", is_rest_day=False, is_blackout=False,
            status="completed", actual_workout_session_id=1,
        ))
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": goal_id, "exercise_id": seeded_history.id,
                "levers": [],
                "applied_snapshot": {
                    "days": [
                        {"target_date": touched_date.isoformat(), "plan_id": None,
                         "day_tag": "push", "micro_tag": "medium", "meso_tag": "medium",
                         "mesocycle_phase_number": None, "is_rest_day": False, "touched": True},
                        {"target_date": untouched_date.isoformat(), "plan_id": None,
                         "day_tag": "pull", "micro_tag": "medium", "meso_tag": "medium",
                         "mesocycle_phase_number": None, "is_rest_day": False, "touched": False},
                    ],
                    "created_plan_ids": [],
                },
            },
            status=periodization_params.STATUS_ACCEPTED,
            decided_at=datetime.now(timezone.utc),
        )
        db.add(proposal)
        await db.commit()
        goal = await db.get(UserGoal, goal_id)
        ctx = await build_context(db, test_user.id, goal, date.today())

    assert ctx["last_applied"]["can_undo"] is True
    assert ctx["last_applied"]["undo_blocked_reason"] is None


async def test_can_undo_false_when_touched_day_has_fact(
    test_user, seeded_history, active_block
):
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_id = await _goal(test_user.id, seeded_history.id)

    touched_date = date.today() + timedelta(days=1)
    async with SessionLocal() as db:
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=touched_date,
            block_id=active_block.id, day_tag="push", micro_tag="medium",
            meso_tag="medium", is_rest_day=False, is_blackout=False,
            status="completed", actual_workout_session_id=1,
        ))
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": goal_id, "exercise_id": seeded_history.id,
                "levers": [],
                "applied_snapshot": {
                    "days": [
                        {"target_date": touched_date.isoformat(), "plan_id": None,
                         "day_tag": "push", "micro_tag": "medium", "meso_tag": "medium",
                         "mesocycle_phase_number": None, "is_rest_day": False, "touched": True},
                    ],
                    "created_plan_ids": [],
                },
            },
            status=periodization_params.STATUS_ACCEPTED,
            decided_at=datetime.now(timezone.utc),
        )
        db.add(proposal)
        await db.commit()
        goal = await db.get(UserGoal, goal_id)
        ctx = await build_context(db, test_user.id, goal, date.today())

    assert ctx["last_applied"]["can_undo"] is False
    assert ctx["last_applied"]["undo_blocked_reason"]
