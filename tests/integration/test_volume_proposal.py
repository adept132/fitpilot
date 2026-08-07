"""Материализация обзора объёма и применение трёх рычагов."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api.services.models import (
    AppUserProfile,
    PeriodizationProposal,
    UserCalendarDay,
)
from api.services.periodization import params as periodization_params
from api.services.volume.repository import utc_today
from api.services.volume.service import apply_volume_decision, refresh_volume_proposals

pytestmark = pytest.mark.asyncio


async def _seed_two_windows(db, user_id, block_id, plan_id, first_start):
    """Два подряд закрытых микроцикла по 4 дня, все дни выполнены."""
    for window in range(2):
        for offset in range(4):
            db.add(UserCalendarDay(
                app_user_id=user_id,
                target_date=first_start + timedelta(days=window * 4 + offset),
                block_id=block_id, plan_id=plan_id, day_tag="push",
                micro_tag="medium", meso_tag="medium",
                microcycle_day_number=offset + 1,
                is_rest_day=False, is_blackout=False, status="completed",
            ))
    # Следующее окно — будущее, ещё открытое.
    for offset in range(4):
        db.add(UserCalendarDay(
            app_user_id=user_id,
            target_date=first_start + timedelta(days=8 + offset),
            block_id=block_id, plan_id=plan_id, day_tag="push",
            micro_tag="medium", meso_tag="medium",
            microcycle_day_number=offset + 1,
            is_rest_day=False, is_blackout=False, status="planned",
        ))
    await db.commit()


async def test_refresh_creates_single_proposal_for_closed_window(
    db, test_user, active_block, seeded_plan
):
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    )
    db.add(profile)
    first_start = utc_today() - timedelta(days=9)
    await _seed_two_windows(db, test_user.id, active_block.id, seeded_plan.id, first_start)

    proposal = await refresh_volume_proposals(db, test_user.id, utc_today())
    await db.commit()

    assert proposal is not None
    assert proposal.kind == periodization_params.KIND_VOLUME_REVIEW
    assert proposal.status == periodization_params.STATUS_PENDING
    assert proposal.payload["adjustments"], "правки должны быть перечислены в payload"
    assert "window_id" in proposal.payload


async def test_refresh_is_idempotent_for_the_same_window(
    db, test_user, active_block, seeded_plan
):
    db.add(AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    ))
    first_start = utc_today() - timedelta(days=9)
    await _seed_two_windows(db, test_user.id, active_block.id, seeded_plan.id, first_start)

    await refresh_volume_proposals(db, test_user.id, utc_today())
    await db.commit()
    await refresh_volume_proposals(db, test_user.id, utc_today())
    await db.commit()

    rows = (await db.execute(
        select(PeriodizationProposal).where(
            PeriodizationProposal.app_user_id == test_user.id,
            PeriodizationProposal.kind == periodization_params.KIND_VOLUME_REVIEW,
        )
    )).scalars().all()
    assert len(rows) == 1


async def test_apply_budget_lever_moves_weekly_target(db, test_user, active_block):
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 25, "min_floor": 6}}},
    )
    db.add(profile)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW,
        reason_code="above_mrv",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "budget_to_range", "muscle": "chest",
             "reason_code": "above_mrv", "delta_sets": -7},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    result = await apply_volume_decision(
        db, test_user.id, proposal, "apply_volume", {"accepted": [0]}
    )
    await db.commit()
    await db.refresh(profile)

    assert result["status"] == "applied"
    assert profile.volume_budget["weekly_targets"]["chest"]["target_sets"] == 18


async def test_apply_prescription_lever_writes_day_adjustments(
    db, test_user, active_block, seeded_plan, seeded_history
):
    db.add(AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    ))
    future = utc_today() + timedelta(days=1)
    day = UserCalendarDay(
        app_user_id=test_user.id, target_date=future, block_id=active_block.id,
        plan_id=seeded_plan.id, day_tag="push", micro_tag="medium",
        meso_tag="medium", microcycle_day_number=1,
        is_rest_day=False, is_blackout=False, status="planned",
    )
    db.add(day)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW,
        reason_code="below_mev",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "prescription_add", "muscle": "chest",
             "reason_code": "below_mev", "delta_sets": 2,
             "exercise_id": seeded_history.id, "day_id": None},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()
    await db.refresh(day)
    proposal.payload["adjustments"][0]["day_id"] = day.id

    await apply_volume_decision(
        db, test_user.id, proposal, "apply_volume", {"accepted": [0]}
    )
    await db.commit()
    await db.refresh(day)

    assert day.volume_adjustments == [
        {"exercise_id": seeded_history.id, "delta_sets": 2, "proposal_id": proposal.id}
    ]


async def test_unaccepted_adjustments_are_not_applied(db, test_user, active_block):
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 25, "min_floor": 6}}},
    )
    db.add(profile)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW, reason_code="above_mrv",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "budget_to_range", "muscle": "chest",
             "reason_code": "above_mrv", "delta_sets": -7},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    await apply_volume_decision(
        db, test_user.id, proposal, "apply_volume", {"accepted": []}
    )
    await db.commit()
    await db.refresh(profile)

    assert profile.volume_budget["weekly_targets"]["chest"]["target_sets"] == 25
