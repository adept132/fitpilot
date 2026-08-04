"""Переключатель фазы двигает блок, а не второй источник правды."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    Mesocycle,
    MesocyclePhase,
    TrainingBlock,
    UserCalendarDay,
)
from api.services.periodization.repository import ensure_active_block


async def _seed(db, user_id: int):
    meso = Mesocycle(author_id=user_id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=3)
    db.add(meso)
    await db.flush()
    for number, tier in enumerate(["easy", "medium", "deload"], start=1):
        db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=number, name=tier, effort_tier=tier))
    db.add(AppUserMesocycle(
        app_user_id=user_id, mesocycle_id=meso.id, is_active=True, microcycle_length=7, current_phase=1
    ))
    db.add(AppUserMicrocycle(
        app_user_id=user_id, name="М", length_days=7,
        days_mapping={"1": {"type": "hard", "tag": "full"}}, is_active=True,
    ))
    await db.commit()
    return await ensure_active_block(db, user_id, date.today())


@pytest.mark.asyncio
async def test_switching_phase_moves_the_block_start(client, db, test_user: AppUser):
    block = await _seed(db, test_user.id)
    original_start = block.start_date

    response = await client.post("/workout-center/active-mesocycle/phase", json={"phase": 3})

    assert response.status_code == 200
    await db.refresh(block)
    # Третья фаза начинается на 15-й день: чтобы «сегодня» стало её первым днём,
    # старт блока уезжает на 14 дней назад.
    assert block.start_date == original_start - timedelta(days=14)


@pytest.mark.asyncio
async def test_context_exposes_the_block(client, db, test_user: AppUser):
    await _seed(db, test_user.id)

    response = await client.get("/workout-center/context")

    assert response.status_code == 200
    body = response.json()
    assert body["active_block"] is not None
    assert body["active_block"]["block_index"] == 1
    assert body["active_block"]["phases_total"] == 3


@pytest.mark.asyncio
async def test_unknown_phase_is_rejected(client, db, test_user: AppUser):
    await _seed(db, test_user.id)
    response = await client.post("/workout-center/active-mesocycle/phase", json={"phase": 99})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_phase_switch_keeps_block_alive_for_ensure_active_block(client, db, test_user: AppUser):
    """Поправка 3 брифа Задачи 13.

    Сдвиг start_date не имеет права столкнуть planned_end_date в прошлое:
    если бы offset_days считался неверно (например, включая длину самой
    целевой фазы, а не только фаз ПЕРЕД ней), planned_end_date мог бы
    оказаться раньше сегодняшнего дня, и первый же вызов ensure_active_block
    закрыл бы блок автопереходом вместо простого переключения фазы —
    пользователь нажал «перейти на фазу 3», а получил новый блок с новым id.
    """
    block = await _seed(db, test_user.id)

    response = await client.post("/workout-center/active-mesocycle/phase", json={"phase": 3})
    assert response.status_code == 200

    await db.refresh(block)
    assert block.planned_end_date >= date.today()
    assert block.status == "active"

    # ensure_active_block — та самая функция, которую вызывает КАЖДЫЙ путь,
    # создающий свободную тренировку или достраивающий календарь. Она обязана
    # вернуть ТОТ ЖЕ блок, а не закрыть его и завести следующий.
    active = await ensure_active_block(db, test_user.id, date.today())
    assert active is not None
    assert active.id == block.id
    assert active.block_index == block.block_index
    assert active.status == "active"
