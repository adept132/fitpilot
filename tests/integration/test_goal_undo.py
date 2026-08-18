"""Откат применённого предложения (P0-12, Задача 10)."""
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from api.services.goal.service import apply_goal_decision, undo_goal_decision
from api.services.models import (
    PeriodizationProposal,
    UserCalendarDay,
    UserExercisePreference,
)
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_soft_lever_writes(test_user):
    """Снести преференции, которые тест навешал на fresh_exercise.

    Без этого teardown test_user падает ForeignKeyViolationError: он сносит
    Exercise до того, как что-то удалит ссылающуюся на неё
    UserExercisePreference (у её exercise_id нет ON DELETE CASCADE). Фикстура
    завязана на test_user, поэтому её teardown по LIFO гарантированно
    отрабатывает раньше teardown'а test_user (тот же приём, что в
    test_goal_apply_soft.py, Задача 8).
    """
    yield
    async with SessionLocal() as db:
        await db.execute(
            delete(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )
        await db.commit()


@pytest_asyncio.fixture
async def active_block(test_user):
    from api.services.models import TrainingBlock
    async with SessionLocal() as db:
        block = TrainingBlock(
            app_user_id=test_user.id, block_index=1, phases=[],
            microcycle_length=7, start_date=date.today(),
            planned_end_date=date.today() + timedelta(days=28), status="active",
        )
        db.add(block)
        await db.commit()
        await db.refresh(block)
        yield block


async def test_undo_restores_previous_preference(test_user, fresh_exercise, active_block):
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="lift_missing",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "ensure_present",
                            "reason_code": "lift_missing", "effect_slope": 0.2,
                            "effect_days": 9, "detail": {}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
        result = await undo_goal_decision(db, test_user.id, proposal)
        await db.commit()

    assert result["status"] == "undone"
    async with SessionLocal() as db:
        prefs = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )).scalars().all()
    assert prefs == []


async def test_undo_is_blocked_after_fact_appears(
    test_user, fresh_exercise, active_block
):
    async with SessionLocal() as db:
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=date.today() + timedelta(days=1),
            block_id=active_block.id, day_tag="push", micro_tag="medium",
            meso_tag="medium", is_rest_day=False, is_blackout=False,
            status="completed", actual_workout_session_id=1,
        ))
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [],
                "applied_snapshot": {
                    "preference": None, "rep_override": None, "scheme": None,
                    "days": [{"target_date": (date.today() + timedelta(days=1)).isoformat(),
                              "plan_id": None, "day_tag": "push", "micro_tag": "medium",
                              "meso_tag": "medium", "mesocycle_phase_number": None,
                              "is_rest_day": False}],
                    "created_plan_ids": [],
                },
            },
            status=periodization_params.STATUS_ACCEPTED,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        result = await undo_goal_decision(db, test_user.id, proposal)

    assert result["status"] == "conflict"
    assert "факт" in result["reason"].lower()


async def test_undo_keeps_manual_changes(test_user, fresh_exercise, active_block):
    """Пользователь после применения сам сменил преференцию — откат её не трогает."""
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="lift_missing",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "ensure_present",
                            "reason_code": "lift_missing", "effect_slope": 0.2,
                            "effect_days": 9, "detail": {}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()

        pref = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )).scalar_one()
        pref.preference = "disliked"
        await db.commit()

        result = await undo_goal_decision(db, test_user.id, proposal)
        await db.commit()

    assert "preference" in result["kept"]
    async with SessionLocal() as db:
        pref = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )).scalar_one()
    assert pref.preference == "disliked"
