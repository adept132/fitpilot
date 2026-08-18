"""Создание предложения автопилота: пороги, дедуп, вытеснение (P0-12, Задача 6)."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api.services.goal.service import refresh_goal_proposals
from api.services.models import PeriodizationProposal, UserExerciseProgressionState, UserGoal
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _primary_goal(
    user_id: int, exercise_id: int, target: float, deadline=None, target_reps: int = 3
) -> int:
    async with SessionLocal() as db:
        goal = UserGoal(
            app_user_id=user_id, goal_type="strength", target_value=target,
            exercise_id=exercise_id, target_reps=target_reps,
            deadline=deadline if deadline is not None else date.today() + timedelta(days=60),
            is_primary=True,
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


async def test_no_primary_goal_means_no_proposal(test_user):
    async with SessionLocal() as db:
        assert await refresh_goal_proposals(db, test_user.id, date.today()) is None


async def test_goal_without_data_produces_nothing(test_user, fresh_exercise):
    await _primary_goal(test_user.id, fresh_exercise.id, 200.0)
    async with SessionLocal() as db:
        assert await refresh_goal_proposals(db, test_user.id, date.today()) is None


async def test_repeated_refresh_does_not_duplicate(test_user, fresh_exercise, seeded_history):
    await _primary_goal(test_user.id, seeded_history.id, 200.0)
    async with SessionLocal() as db:
        await refresh_goal_proposals(db, test_user.id, date.today())
        await refresh_goal_proposals(db, test_user.id, date.today())

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.app_user_id == test_user.id,
                PeriodizationProposal.kind == periodization_params.KIND_GOAL_PLAN,
                PeriodizationProposal.status == periodization_params.STATUS_PENDING,
            )
        )).scalars().all()
    assert len(rows) <= 1


# --- Сверх брифа: финиш ведущей цели снимает is_primary (спека §7) ---
#
# Бриф Задачи 6 этого не покрывает — требование добавлено постановщиком
# отдельно (см. p0-12-task-6-report.md, раздел "Сверх брифа"). Достигнутая
# или просроченная ведущая цель обязана освободить свой единственный слот,
# иначе пользователь никогда не получит предложения выбрать следующую.

async def test_achieved_goal_clears_primary_flag(test_user, fresh_exercise):
    """target_reps=3, target_value=100 -> target_e1rm = 100 * 1.1 = 110.
    working_e1rm=150, с явным запасом над целью — цель достигнута."""
    goal_id = await _primary_goal(test_user.id, fresh_exercise.id, 100.0, target_reps=3)
    await _set_working_e1rm(test_user.id, fresh_exercise.id, 150.0)

    async with SessionLocal() as db:
        result = await refresh_goal_proposals(db, test_user.id, date.today())
    assert result is None

    async with SessionLocal() as db:
        goal = await db.get(UserGoal, goal_id)
        assert goal.is_primary is False


async def test_overdue_goal_clears_primary_flag(test_user, fresh_exercise):
    """Дедлайн уже прошёл — цель просрочена, даже если e1RM не достигнут."""
    past_deadline = date.today() - timedelta(days=1)
    goal_id = await _primary_goal(
        test_user.id, fresh_exercise.id, 200.0, deadline=past_deadline
    )

    async with SessionLocal() as db:
        result = await refresh_goal_proposals(db, test_user.id, date.today())
    assert result is None

    async with SessionLocal() as db:
        goal = await db.get(UserGoal, goal_id)
        assert goal.is_primary is False


async def test_finished_goal_expires_its_active_proposal(test_user, fresh_exercise, active_block):
    """Активное goal_plan-предложение достигнутой цели уходит в expired, а не
    остаётся висеть pending навсегда."""
    goal_id = await _primary_goal(test_user.id, fresh_exercise.id, 100.0, target_reps=3)
    await _set_working_e1rm(test_user.id, fresh_exercise.id, 150.0)

    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id,
            block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN,
            reason_code="pace_behind",
            payload={"goal_id": goal_id, "exercise_id": fresh_exercise.id, "inputs_hash": "old"},
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        proposal_id = proposal.id

    async with SessionLocal() as db:
        result = await refresh_goal_proposals(db, test_user.id, date.today())
    assert result is None

    async with SessionLocal() as db:
        refreshed = await db.get(PeriodizationProposal, proposal_id)
        assert refreshed.status == periodization_params.STATUS_EXPIRED
