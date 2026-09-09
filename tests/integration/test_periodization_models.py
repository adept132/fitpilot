"""Схема P0-08: блок переживает удаление шаблона, поля читаются обратно."""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import delete, select

from api.services.models import (
    AppUser,
    Mesocycle,
    PeriodizationProposal,
    TrainingBlock,
)


@pytest.mark.asyncio
async def test_block_survives_template_deletion(db, test_user: AppUser):
    meso = Mesocycle(
        author_id=test_user.id,
        name="Тестовая стратегия",
        code=f"test_{uuid.uuid4().hex[:8]}",
        phases_in_cycle=2,
    )
    db.add(meso)
    await db.flush()

    block = TrainingBlock(
        phase_snapshot_trusted=True,
        app_user_id=test_user.id,
        block_index=1,
        mesocycle_id=meso.id,
        phases=[
            {"phase_number": 1, "name": "Накопление", "effort_tier": "medium", "length_days": 7},
            {"phase_number": 2, "name": "Разгрузка", "effort_tier": "deload", "length_days": 7},
        ],
        microcycle_length=7,
        start_date=date(2026, 8, 3),
        planned_end_date=date(2026, 8, 16),
        status="active",
    )
    db.add(block)
    await db.commit()
    block_id = block.id

    await db.execute(delete(Mesocycle).where(Mesocycle.id == meso.id))
    await db.commit()
    db.expire_all()

    survived = (
        await db.execute(select(TrainingBlock).where(TrainingBlock.id == block_id))
    ).scalar_one()
    assert survived.mesocycle_id is None, "шаблон должен отвязаться, а не унести блок"
    assert len(survived.phases) == 2
    assert survived.phases[1]["effort_tier"] == "deload"

    await db.execute(delete(TrainingBlock).where(TrainingBlock.id == block_id))
    await db.commit()


@pytest.mark.asyncio
async def test_proposal_roundtrip(db, test_user: AppUser):
    block = TrainingBlock(
        phase_snapshot_trusted=True,
        app_user_id=test_user.id,
        block_index=1,
        phases=[{"phase_number": 1, "name": "База", "effort_tier": "medium", "length_days": 7}],
        microcycle_length=7,
        start_date=date(2026, 8, 3),
        planned_end_date=date(2026, 8, 9),
        status="active",
    )
    db.add(block)
    await db.flush()

    proposal = PeriodizationProposal(
        app_user_id=test_user.id,
        block_id=block.id,
        kind="early_deload",
        reason_code="fatigue_high",
        payload={"fatigued_days": 6},
        status="pending",
    )
    db.add(proposal)
    await db.commit()
    proposal_id = proposal.id

    stored = (
        await db.execute(
            select(PeriodizationProposal).where(PeriodizationProposal.id == proposal_id)
        )
    ).scalar_one()
    assert stored.status == "pending"
    assert stored.payload["fatigued_days"] == 6
    assert stored.decided_at is None

    await db.execute(delete(PeriodizationProposal).where(PeriodizationProposal.id == proposal_id))
    await db.execute(delete(TrainingBlock).where(TrainingBlock.id == block.id))
    await db.commit()
