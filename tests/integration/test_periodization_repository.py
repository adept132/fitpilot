"""Создание блока из активных настроек и снимок состояния прогрессии."""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import delete, select

from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    Mesocycle,
    MesocyclePhase,
    TrainingBlock,
)
from api.services.periodization.repository import (
    block_state,
    ensure_active_block,
    get_active_block,
)

TODAY = date(2026, 8, 3)


async def _seed_periodization(db, user_id: int) -> None:
    meso = Mesocycle(
        author_id=user_id,
        name="Тест",
        code=f"t_{uuid.uuid4().hex[:8]}",
        phases_in_cycle=3,
    )
    db.add(meso)
    await db.flush()
    for number, tier in enumerate(["easy", "medium", "deload"], start=1):
        db.add(
            MesocyclePhase(
                mesocycle_id=meso.id, phase_number=number, name=tier, effort_tier=tier
            )
        )
    db.add(
        AppUserMesocycle(
            app_user_id=user_id,
            mesocycle_id=meso.id,
            is_active=True,
            microcycle_length=7,
            current_phase=1,
        )
    )
    db.add(
        AppUserMicrocycle(
            app_user_id=user_id,
            name="Тестовый микроцикл",
            length_days=7,
            days_mapping={"1": {"type": "hard", "tag": "push"}},
            is_active=True,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_ensure_creates_first_block_from_active_settings(db, test_user: AppUser):
    await _seed_periodization(db, test_user.id)

    block = await ensure_active_block(db, test_user.id, TODAY)

    assert block is not None
    assert block.block_index == 1
    assert block.start_date == TODAY, "первый блок начинается сегодня, прошлое не реконструируем"
    assert block.microcycle_length == 7
    assert [p["effort_tier"] for p in block.phases] == ["easy", "medium", "deload"]
    assert block.planned_end_date == date(2026, 8, 23)  # 21 день, включая первый
    assert block.status == "active"


@pytest.mark.asyncio
async def test_ensure_is_idempotent(db, test_user: AppUser):
    await _seed_periodization(db, test_user.id)

    first = await ensure_active_block(db, test_user.id, TODAY)
    second = await ensure_active_block(db, test_user.id, date(2026, 8, 10))

    assert first.id == second.id
    count = len(
        (
            await db.execute(
                select(TrainingBlock).where(TrainingBlock.app_user_id == test_user.id)
            )
        ).scalars().all()
    )
    assert count == 1


@pytest.mark.asyncio
async def test_no_periodization_means_no_block(db, test_user: AppUser):
    """Пустая координата честнее выдуманной."""
    assert await ensure_active_block(db, test_user.id, TODAY) is None
    assert await get_active_block(db, test_user.id) is None


@pytest.mark.asyncio
async def test_block_state_reads_the_snapshot_back(db, test_user: AppUser):
    await _seed_periodization(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)

    state = block_state(block)

    assert state.block_index == 1
    assert [p.effort_tier for p in state.phases] == ["easy", "medium", "deload"]
    assert state.start_date == TODAY
