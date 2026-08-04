"""Жизненный цикл блока: смена сплита и долгий перерыв (P0-08, Задача 14)."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

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
)
from api.services.periodization import params
from api.services.periodization.repository import ensure_active_block
from api.services.periodization.service import close_stale_block


async def _seed(db, user_id: int, start: date):
    meso = Mesocycle(author_id=user_id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=1)
    db.add(meso)
    await db.flush()
    db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=1, name="Средняя", effort_tier="medium"))
    db.add(AppUserMesocycle(
        app_user_id=user_id, mesocycle_id=meso.id, is_active=True, microcycle_length=7, current_phase=1
    ))
    db.add(AppUserMicrocycle(
        app_user_id=user_id, name="М", length_days=7,
        days_mapping={"1": {"type": "hard", "tag": "full"}}, is_active=True,
    ))
    await db.commit()
    return await ensure_active_block(db, user_id, start)


async def _seed_split_blueprint(db, user_id: int):
    """Минимальный сплит из одного дня — нужен /splits/launch, чтобы вообще
    развернуть расписание (пустой blueprint.slots роняет эндпоинт с 400)."""
    blueprint = SplitBlueprint(
        name=f"Сплит {uuid.uuid4().hex[:8]}", author_id=user_id, length_days=1, is_system=False,
    )
    day = DayBlueprint(
        name="Full Body", author_id=user_id, template_type=DayTemplateType.FULL_BODY, is_system=False,
    )
    db.add_all([blueprint, day])
    await db.flush()
    db.add(DayMuscleTarget(day_id=day.id, muscle_group_id="full_body"))
    db.add(SplitDaySlot(blueprint_id=blueprint.id, day_id=day.id, day_order=0))
    await db.commit()
    return blueprint


# --- Долгий перерыв ----------------------------------------------------------


@pytest.mark.asyncio
async def test_long_layoff_closes_the_block(db, test_user: AppUser):
    block = await _seed(db, test_user.id, date(2026, 5, 1))

    closed = await close_stale_block(db, test_user.id, date(2026, 8, 3))

    assert closed is not None
    await db.refresh(block)
    assert block.status == "closed"
    assert block.close_reason == params.CLOSE_LAYOFF


@pytest.mark.asyncio
async def test_fresh_block_is_not_closed(db, test_user: AppUser):
    block = await _seed(db, test_user.id, date(2026, 8, 1))

    assert await close_stale_block(db, test_user.id, date(2026, 8, 3)) is None
    await db.refresh(block)
    assert block.status == "active"


@pytest.mark.asyncio
async def test_block_just_past_its_end_is_not_closed_yet(db, test_user: AppUser):
    """Блок закончился вчера — это граница, а не заброшенность: пользователь
    должен успеть увидеть итоги и решить сам."""
    block = await _seed(db, test_user.id, date(2026, 7, 20))

    assert await close_stale_block(db, test_user.id, date(2026, 7, 28)) is None
    await db.refresh(block)
    assert block.status == "active"


@pytest.mark.asyncio
async def test_context_endpoint_actually_wires_close_stale_block(client, db, test_user: AppUser):
    """Не только прямой вызов close_stale_block — а и её реальное подключение
    в refresh_proposals, ДО roll_over_if_complete (поправка 2 брифа Задачи 14).
    GET /periodization/context идёт через safe_refresh_proposals ->
    refresh_proposals, ровно тем же путём, что и мобильный клиент."""
    block = await _seed(db, test_user.id, date(2026, 5, 1))

    response = await client.get(
        "/periodization/context", params={"local_date": "2026-08-03"}
    )
    assert response.status_code == 200

    await db.refresh(block)
    assert block.status == "closed"
    assert block.close_reason == params.CLOSE_LAYOFF


# --- Смена сплита -------------------------------------------------------------


@pytest.mark.asyncio
async def test_split_change_closes_active_block_and_starts_next_at_new_split_date(
    client, db, test_user: AppUser
):
    """Поправки 3+4 брифа Задачи 14: смена сплита обязана закрыть активный
    блок ДО того, как launch_and_unroll_plan развернёт расписание (иначе
    ensure_active_block внутри него найдёт СТАРЫЙ блок), а следующий блок
    обязан стартовать с даты НОВОГО сплита — она может быть в будущем
    (пользователь планирует запуск наперёд), а не с сегодня."""
    block = await _seed(db, test_user.id, date(2026, 8, 1))
    blueprint = await _seed_split_blueprint(db, test_user.id)
    future_start = date(2026, 8, 20)

    response = await client.post(
        "/splits/launch",
        json={
            "blueprint_id": str(blueprint.id),
            "start_date": future_start.isoformat(),
            "blackout_weekdays": [],
        },
    )
    assert response.status_code == 200, response.text

    await db.refresh(block)
    assert block.status == "closed"
    assert block.close_reason == params.CLOSE_SPLIT_CHANGED

    next_block = (
        await db.execute(
            select(TrainingBlock).where(
                TrainingBlock.app_user_id == test_user.id,
                TrainingBlock.status == "active",
            )
        )
    ).scalars().first()
    assert next_block is not None
    assert next_block.id != block.id
    assert next_block.block_index == block.block_index + 1
    assert next_block.start_date == future_start


@pytest.mark.asyncio
async def test_split_change_without_periodization_still_works(client, db, test_user: AppUser):
    """Особое внимание брифа Задачи 14: у пользователя без настроенной
    периодизации блока нет — закрывать нечего, старый путь запуска сплита
    обязан отработать как раньше."""
    blueprint = await _seed_split_blueprint(db, test_user.id)

    response = await client.post(
        "/splits/launch",
        json={
            "blueprint_id": str(blueprint.id),
            "start_date": date(2026, 8, 10).isoformat(),
            "blackout_weekdays": [],
        },
    )
    assert response.status_code == 200, response.text

    blocks = (
        await db.execute(
            select(TrainingBlock).where(TrainingBlock.app_user_id == test_user.id)
        )
    ).scalars().all()
    assert blocks == []
