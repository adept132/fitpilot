"""Мягкие рычаги: применение, снимок, выборочность (P0-12, Задача 8)."""
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from api.services.goal.service import apply_goal_decision
from api.services.models import (
    AppUserProfile,
    PeriodizationProposal,
    UserExercisePreference,
    UserExerciseRepOverride,
)
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_soft_lever_writes(test_user):
    """Снести преференции/оверрайды, которые тест навешал на fresh_exercise.

    Без этого teardown test_user падает ForeignKeyViolationError: он сносит
    Exercise до того, как что-то удалит ссылающиеся на неё
    UserExercisePreference/UserExerciseRepOverride (у их exercise_id нет
    ON DELETE CASCADE). Фикстура завязана на test_user, поэтому её teardown
    по LIFO гарантированно отрабатывает раньше teardown'а test_user.
    """
    yield
    async with SessionLocal() as db:
        await db.execute(
            delete(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )
        await db.execute(
            delete(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id
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


async def _proposal(user_id: int, block_id: int, exercise_id: int) -> int:
    async with SessionLocal() as db:
        row = PeriodizationProposal(
            app_user_id=user_id, block_id=block_id,
            kind=periodization_params.KIND_GOAL_PLAN,
            reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": exercise_id,
                "levers": [
                    {"index": 0, "kind": "ensure_present", "reason_code": "lift_missing",
                     "effect_slope": 0.2, "effect_days": 10, "detail": {}},
                    {"index": 1, "kind": "rep_range", "reason_code": "pace_behind",
                     "effect_slope": 0.1, "effect_days": 5,
                     "detail": {"rep_min": 2, "rep_max": 5}},
                ],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def test_only_accepted_levers_are_applied(test_user, fresh_exercise, active_block):
    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
    assert result["applied"] == [0]

    async with SessionLocal() as db:
        prefs = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )).scalars().all()
        overrides = (await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id
            )
        )).scalars().all()
    assert len(prefs) == 1
    assert overrides == []


async def test_empty_accepted_applies_nothing(test_user, fresh_exercise, active_block):
    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": []},
        )
    assert result["status"] == "declined"
    assert result["applied"] == []


async def test_snapshot_records_previous_state(test_user, fresh_exercise, active_block):
    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0, 1]},
        )

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot = row.payload["applied_snapshot"]
    assert snapshot is not None
    assert snapshot["preference"] is None      # преференции не было
    assert snapshot["rep_override"] is None    # оверрайда не было
