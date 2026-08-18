"""Структурный рычаг не трогает дни с фактом (P0-12, Задача 9).

apply_goal_decision НЕ коммитит сессию сама (см. её докстринг и
tests/integration/test_goal_apply_soft.py) — каждый блок ниже, желающий
увидеть эффект в следующем блоке, коммитит явно, ровно как это сделает
настоящий вызывающий (periodization.service.apply_decision, Задача 10).
"""
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from api.services.goal.service import apply_goal_decision
from api.services.models import PeriodizationProposal, TrainingBlock, UserCalendarDay
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def active_block(test_user):
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


@pytest_asyncio.fixture
async def seeded_future_days(test_user, active_block):
    """Пять будущих дней блока; один из них уже выполнен."""
    async with SessionLocal() as db:
        for i in range(5):
            db.add(UserCalendarDay(
                app_user_id=test_user.id,
                target_date=date.today() + timedelta(days=i + 1),
                block_id=active_block.id, day_tag="push",
                micro_tag="medium", meso_tag="medium",
                is_rest_day=False, is_blackout=False,
                status="completed" if i == 2 else "planned",
            ))
        await db.commit()
    yield


async def test_completed_days_survive_regeneration(
    test_user, fresh_exercise, active_block, seeded_future_days
):
    """Один из будущих дней помечен completed — регенерация обязана его сохранить."""
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "lift_frequency",
                            "reason_code": "pace_behind", "effect_slope": 0.3,
                            "effect_days": 12, "detail": {"delta_sessions": 1}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)

        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        # apply_goal_decision больше не коммитит сама — коммитим здесь явно,
        # ровно как это сделает настоящий вызывающий (Задача 10).
        await db.commit()

    assert result["skipped_days"] >= 1

    async with SessionLocal() as db:
        surviving = (await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.status == "completed",
            )
        )).scalars().all()
    assert len(surviving) >= 1


async def test_snapshot_keeps_previous_day_coordinates(
    test_user, fresh_exercise, active_block, seeded_future_days
):
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "lift_frequency",
                            "reason_code": "pace_behind", "effect_slope": 0.3,
                            "effect_days": 12, "detail": {"delta_sessions": 1}}],
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
        # apply_goal_decision больше не коммитит сама — коммитим здесь явно,
        # ровно как это сделает настоящий вызывающий (Задача 10).
        await db.commit()
        pid = proposal.id

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    days = row.payload["applied_snapshot"]["days"]
    assert days
    assert all({"target_date", "plan_id", "day_tag"} <= set(d) for d in days)
