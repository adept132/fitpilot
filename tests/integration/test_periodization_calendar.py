"""Календарь берёт фазу из снимка блока, а не из формулы по шаблону."""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import delete, select

from api.services.day_template import DayTemplateType
from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    DayBlueprint,
    DayMuscleTarget,
    Mesocycle,
    MesocyclePhase,
    SplitBlueprint,
    SplitDaySlot,
    TrainingBlock,
    UserCalendarDay,
    UserSplit,
)
from api.services.periodization.repository import ensure_active_block
from api.services.scheduling_engine import SchedulingEngine

TODAY = date(2026, 8, 3)


async def _seed(db, user_id: int) -> None:
    """Мезоцикл/микроцикл (нужны ensure_active_block) + минимальный сплит
    из одного тренировочного дня (нужен generate_block_days, чтобы вообще
    было что класть в календарь — без активного сплита слот-очередь пуста).

    author_id=user_id у SplitBlueprint/DayBlueprint — не системный шаблон,
    поэтому каскад удаления AppUser (см. teardown фикстуры test_user) уносит
    их сам, без ручной зачистки, как и остальные сущности периодизации в
    этом файле."""
    meso = Mesocycle(
        author_id=user_id, name="Тест", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=2
    )
    db.add(meso)
    await db.flush()
    for number, tier in enumerate(["medium", "deload"], start=1):
        db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=number, name=tier, effort_tier=tier))
    db.add(
        AppUserMesocycle(
            app_user_id=user_id, mesocycle_id=meso.id, is_active=True,
            microcycle_length=7, current_phase=1,
        )
    )
    db.add(
        AppUserMicrocycle(
            app_user_id=user_id, name="Микро", length_days=7,
            days_mapping={str(i): {"type": "hard", "tag": "full"} for i in range(1, 8)},
            is_active=True,
        )
    )

    blueprint = SplitBlueprint(name="Тестовый сплит", author_id=user_id, length_days=1, is_system=False)
    day = DayBlueprint(
        name="Full Body", author_id=user_id, template_type=DayTemplateType.FULL_BODY, is_system=False,
    )
    db.add_all([blueprint, day])
    await db.flush()
    db.add(DayMuscleTarget(day_id=day.id, muscle_group_id="full_body"))
    db.add(SplitDaySlot(blueprint_id=blueprint.id, day_id=day.id, day_order=0))
    db.add(
        UserSplit(
            app_user_id=user_id, blueprint_id=blueprint.id, is_active=True, current_day=1,
            selected_plans={},
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_days_carry_block_id_and_snapshot_tier(db, test_user: AppUser):
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)

    created = await SchedulingEngine.generate_block_days(
        db, test_user.id, block, from_date=TODAY, until_date=block.planned_end_date
    )
    assert created == 14

    days = (
        await db.execute(
            select(UserCalendarDay)
            .where(UserCalendarDay.app_user_id == test_user.id)
            .order_by(UserCalendarDay.target_date)
        )
    ).scalars().all()

    assert all(d.block_id == block.id for d in days)
    assert days[0].meso_tag == "medium"
    assert days[6].meso_tag == "medium"
    assert days[7].meso_tag == "deload", "8-й день блока — вторая фаза"
    assert days[0].mesocycle_phase_number == 1
    assert days[7].mesocycle_phase_number == 2


@pytest.mark.asyncio
async def test_inserted_deload_reaches_the_calendar(db, test_user: AppUser):
    """Правка снимка блока — единственный источник фазы для календаря."""
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)

    block.phases = [
        {"phase_number": 1, "name": "Средняя", "effort_tier": "medium", "length_days": 3},
        {"phase_number": 3, "name": "Разгрузка", "effort_tier": "deload", "length_days": 7},
        {"phase_number": 2, "name": "Разгрузка плановая", "effort_tier": "deload", "length_days": 7},
    ]
    await db.commit()

    await SchedulingEngine.generate_block_days(
        db, test_user.id, block, from_date=TODAY, until_date=date(2026, 8, 9)
    )

    days = (
        await db.execute(
            select(UserCalendarDay)
            .where(UserCalendarDay.app_user_id == test_user.id)
            .order_by(UserCalendarDay.target_date)
        )
    ).scalars().all()

    assert days[2].meso_tag == "medium"
    assert days[3].meso_tag == "deload"
    assert days[3].mesocycle_phase_number == 3, "вставленная фаза сохраняет свой номер"


@pytest.mark.asyncio
async def test_generation_never_touches_the_past(db, test_user: AppUser):
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)

    yesterday = UserCalendarDay(
        app_user_id=test_user.id, target_date=date(2026, 8, 2),
        day_tag="старый день", meso_tag="failure", micro_tag="hard",
        is_rest_day=False, is_blackout=False, status="planned",
    )
    db.add(yesterday)
    await db.commit()

    await SchedulingEngine.generate_block_days(
        db, test_user.id, block, from_date=TODAY, until_date=block.planned_end_date
    )
    await db.refresh(yesterday)

    assert yesterday.meso_tag == "failure"
    assert yesterday.block_id is None
