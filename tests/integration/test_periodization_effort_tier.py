"""Ключевой тест P0-08: вставленная разгрузка обязана дойти до железа.

Если резолвер продолжит читать шаблон, в интерфейсе разгрузка начнётся, а вес
продолжит расти — самый дорогой способ провалить эту задачу.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete

from api.services.models import (
    AppUser,
    AppUserMesocycle,
    Exercise,
    Mesocycle,
    MesocyclePhase,
    TrainingBlock,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.progression.repository import load_history, resolve_phase_effort_tier


@pytest.mark.asyncio
async def test_tier_comes_from_the_block_snapshot(db, test_user: AppUser):
    meso = Mesocycle(
        author_id=test_user.id, name="Тест", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=2
    )
    db.add(meso)
    await db.flush()
    # В ШАБЛОНЕ фазы 3 нет вовсе — она существует только в снимке блока.
    db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=1, name="Средняя", effort_tier="medium"))
    db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=2, name="Тяжёлая", effort_tier="prefailure"))
    user_meso = AppUserMesocycle(
        app_user_id=test_user.id, mesocycle_id=meso.id, is_active=True,
        microcycle_length=7, current_phase=1,
    )
    db.add(user_meso)
    await db.flush()

    block = TrainingBlock(
        app_user_id=test_user.id, block_index=1,
        user_mesocycle_id=user_meso.id, mesocycle_id=meso.id,
        phases=[
            {"phase_number": 1, "name": "Средняя", "effort_tier": "medium", "length_days": 7},
            {"phase_number": 3, "name": "Разгрузка", "effort_tier": "deload", "length_days": 7},
            {"phase_number": 2, "name": "Тяжёлая", "effort_tier": "prefailure", "length_days": 7},
        ],
        microcycle_length=7, start_date=date(2026, 8, 3),
        planned_end_date=date(2026, 8, 23), status="active",
    )
    db.add(block)
    await db.commit()

    tier = await resolve_phase_effort_tier(
        db, user_meso.id, 3, training_block_id=block.id
    )
    assert tier == "deload"

    await db.execute(delete(TrainingBlock).where(TrainingBlock.id == block.id))
    await db.commit()


@pytest.mark.asyncio
async def test_falls_back_to_template_without_block(db, test_user: AppUser):
    """Сессии, созданные до P0-08, не имеют block_id — они обязаны работать."""
    meso = Mesocycle(
        author_id=test_user.id, name="Тест", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=1
    )
    db.add(meso)
    await db.flush()
    db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=1, name="Разгрузка", effort_tier="deload"))
    user_meso = AppUserMesocycle(
        app_user_id=test_user.id, mesocycle_id=meso.id, is_active=True,
        microcycle_length=7, current_phase=1,
    )
    db.add(user_meso)
    await db.commit()

    assert await resolve_phase_effort_tier(db, user_meso.id, 1) == "deload"
    assert await resolve_phase_effort_tier(db, user_meso.id, 1, training_block_id=None) == "deload"


@pytest.mark.asyncio
async def test_history_marks_session_in_inserted_deload_as_deload(db, test_user: AppUser):
    """_load_deload_map должна знать про вставленную разгрузку, как и резолвер.

    Сессия проведена в фазе 3, которой нет в шаблоне (она существует только
    в снимке блока — та же ситуация, что и в test_tier_comes_from_the_block_snapshot).
    Если _load_deload_map по-прежнему резолвит фазу через JOIN с шаблоном
    MesocyclePhase, для пары (app_user_mesocycle_id=meso, phase_number=3) там
    нет строки — is_deload останется False, и rebuild_state засчитает честную
    разгрузку в счёт застоя (sessions_since_gain), хотя веса не росли по
    уважительной причине.
    """
    marker = uuid.uuid4().hex[:8]
    ex = Exercise(
        name=f"Тестовое упражнение для разгрузки {marker}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(ex)
    await db.flush()

    meso = Mesocycle(
        author_id=test_user.id, name="Тест", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=2
    )
    db.add(meso)
    await db.flush()
    # В ШАБЛОНЕ фазы 3 снова нет — join _phase_effort_tier_stmt её не найдёт.
    db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=1, name="Средняя", effort_tier="medium"))
    db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=2, name="Тяжёлая", effort_tier="prefailure"))
    user_meso = AppUserMesocycle(
        app_user_id=test_user.id, mesocycle_id=meso.id, is_active=True,
        microcycle_length=7, current_phase=3,
    )
    db.add(user_meso)
    await db.flush()

    block = TrainingBlock(
        app_user_id=test_user.id, block_index=1,
        user_mesocycle_id=user_meso.id, mesocycle_id=meso.id,
        phases=[
            {"phase_number": 1, "name": "Средняя", "effort_tier": "medium", "length_days": 7},
            {"phase_number": 3, "name": "Разгрузка", "effort_tier": "deload", "length_days": 7},
            {"phase_number": 2, "name": "Тяжёлая", "effort_tier": "prefailure", "length_days": 7},
        ],
        microcycle_length=7, start_date=date(2026, 8, 3),
        planned_end_date=date(2026, 8, 23), status="active",
    )
    db.add(block)
    await db.flush()

    workout = WorkoutSession(
        app_user_id=test_user.id,
        source="free",
        status="finished",
        app_user_mesocycle_id=user_meso.id,
        mesocycle_phase=3,
        training_block_id=block.id,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(workout)
    await db.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id,
        exercise_id=ex.id,
        order_index=0,
    )
    db.add(se)
    await db.flush()

    db.add(
        WorkoutSessionSet(
            workout_session_exercise_id=se.id,
            set_number=1,
            set_type="normal",
            weight=40.0,
            reps=12,
            effort_level="medium",
            is_completed=True,
        )
    )
    await db.commit()

    history = await load_history(db, test_user.id, ex.id)
    assert len(history.sessions) == 1
    assert history.sessions[0].is_deload is True, (
        "сессия во вставленной разгрузке (phase_number=3, есть только в "
        "снимке блока) обязана быть помечена is_deload=True"
    )

    await db.execute(delete(TrainingBlock).where(TrainingBlock.id == block.id))
    await db.execute(delete(Mesocycle).where(Mesocycle.id == meso.id))
    await db.commit()
