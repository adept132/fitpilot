"""Создание блока из активных настроек и снимок состояния прогрессии."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    Exercise,
    Mesocycle,
    MesocyclePhase,
    TrainingBlock,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.periodization import repository as periodization_repository
from api.services.periodization.repository import (
    block_state,
    build_state_snapshot,
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


# --- Ревью Задачи 6: гонка в ensure_active_block -----------------------------


@pytest.mark.asyncio
async def test_ensure_recovers_when_block_appears_concurrently(db, test_user: AppUser):
    """Гонка "проверить — потом создать": мобильный клиент на старте дёргает
    контекст дня и контекст периодизации одновременно, оба запроса видят
    "блока нет", оба считают один и тот же block_index — второй INSERT падает
    на уникальном индексе uq_training_blocks_user_index. ensure_active_block
    обязана не упасть, а вернуть блок, который успел вставить конкурент.

    Настоящую гонку (два реальных параллельных запроса) в тесте не
    воспроизвести — нет гарантии нужного чередования их шагов. Имитируем её:

    - get_active_block патчим ровно так, как просит ревью: первый вызов
      (самая первая проверка "есть ли уже активный блок" в начале
      ensure_active_block) честно отвечает None — ровно то, что увидел бы
      настоящий конкурентный запрос ДО того, как сосед закоммитил свою
      вставку; второй вызов — это уже recovery-путь после пойманного
      IntegrityError, и он делегирует в НАСТОЯЩУЮ (неподмененную) реализацию,
      которая обязана найти блок соседа по-настоящему.
    - одного мока get_active_block недостаточно, чтобы получить настоящий
      IntegrityError: block_index в ensure_active_block всегда считается как
      "текущий максимум + 1" по свежему SELECT, поэтому честный запрос сам по
      себе никогда не выберет уже занятый индекс. Чтобы отразить момент
      гонки, когда сосед вставляет и коммитит свой блок ровно в зазоре между
      нашим чтением last_index и нашей собственной вставкой, патчим
      create_block: он сначала по-настоящему вставляет и коммитит "блок
      соседа" с тем индексом, который ensure_active_block уже решила занять,
      а затем выполняет настоящую попытку ensure_active_block вставить блок с
      тем же индексом — она и падает на уникальном индексе, как в реальной
      гонке.
    """
    await _seed_periodization(db, test_user.id)

    real_get_active_block = periodization_repository.get_active_block
    real_create_block = periodization_repository.create_block
    get_active_block_calls = {"n": 0}

    async def _fake_get_active_block(session, app_user_id):
        get_active_block_calls["n"] += 1
        if get_active_block_calls["n"] == 1:
            return None
        return await real_get_active_block(session, app_user_id)

    async def _fake_create_block(
        session,
        app_user_id,
        start_date,
        *,
        block_index,
        phases,
        user_meso,
        user_micro,
        split_blueprint_id=None,
        entry_state=None,
    ):
        # "Сосед" вставляет и коммитит свой блок первым — с тем же индексом,
        # который наш код уже решил занять.
        await real_create_block(
            session,
            app_user_id,
            start_date,
            block_index=block_index,
            phases=phases,
            user_meso=user_meso,
            user_micro=user_micro,
        )
        await session.commit()
        # Настоящая попытка ensure_active_block вставить блок с тем же
        # индексом — обязана упасть на уникальном индексе.
        return await real_create_block(
            session,
            app_user_id,
            start_date,
            block_index=block_index,
            phases=phases,
            user_meso=user_meso,
            user_micro=user_micro,
            split_blueprint_id=split_blueprint_id,
            entry_state=entry_state,
        )

    with patch(
        "api.services.periodization.repository.get_active_block",
        side_effect=_fake_get_active_block,
    ), patch(
        "api.services.periodization.repository.create_block",
        side_effect=_fake_create_block,
    ):
        block = await ensure_active_block(db, test_user.id, TODAY)

    assert block is not None, "ensure_active_block не должна падать на гонке"
    assert block.block_index == 1

    blocks = (
        await db.execute(
            select(TrainingBlock).where(TrainingBlock.app_user_id == test_user.id)
        )
    ).scalars().all()
    assert len(blocks) == 1, "второй блок в БД появиться не должен"
    assert blocks[0].id == block.id


# --- Ревью Задачи 6: build_state_snapshot ------------------------------------


@pytest.mark.asyncio
async def test_state_snapshot_captures_progression(db: AsyncSession, test_user: AppUser):
    """До этого теста ни один тест файла не создавал завершённых тренировок,
    поэтому тело цикла build_state_snapshot ни разу не выполнялось — главная
    функция задачи была не проверена вообще."""
    marker = uuid.uuid4().hex[:8]
    exercise = Exercise(
        name=f"Тестовое упражнение снимка {marker}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(exercise)
    await db.flush()

    workout = WorkoutSession(
        app_user_id=test_user.id,
        source="free",
        status="finished",
        finished_at=datetime.now(timezone.utc),
    )
    db.add(workout)
    await db.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id, exercise_id=exercise.id, order_index=0
    )
    db.add(se)
    await db.flush()

    for set_number in range(1, 4):
        db.add(
            WorkoutSessionSet(
                workout_session_exercise_id=se.id,
                set_number=set_number,
                set_type="normal",
                weight=60.0,
                reps=8,
                effort_level="medium",
                is_completed=True,
            )
        )
    await db.commit()

    try:
        snapshot = await build_state_snapshot(db, test_user.id, [exercise.id])

        key = str(exercise.id)
        assert key in snapshot
        assert snapshot[key]["completed_sessions"] != 0
        assert snapshot[key]["working_e1rm"] > 0
    finally:
        # Порядок важен (тот же, что в teardown фикстуры test_user в
        # conftest.py): подходы -> упражнения сессии -> сессия -> упражнение.
        await db.execute(
            delete(WorkoutSessionSet).where(
                WorkoutSessionSet.workout_session_exercise_id == se.id
            )
        )
        await db.execute(
            delete(WorkoutSessionExercise).where(WorkoutSessionExercise.id == se.id)
        )
        await db.execute(delete(WorkoutSession).where(WorkoutSession.id == workout.id))
        await db.execute(delete(Exercise).where(Exercise.id == exercise.id))
        await db.commit()


@pytest.mark.asyncio
async def test_state_snapshot_skips_exercises_without_history(
    db: AsyncSession, test_user: AppUser
):
    """Упражнение без единой завершённой сессии не должно попасть в снимок
    вовсе — отсутствие ключа, а не запись с пустыми значениями."""
    marker = uuid.uuid4().hex[:8]
    exercise = Exercise(
        name=f"Тестовое упражнение без истории для снимка {marker}",
        category="base",
        main_muscle_group="back",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(exercise)
    await db.commit()

    try:
        snapshot = await build_state_snapshot(db, test_user.id, [exercise.id])
        assert str(exercise.id) not in snapshot
    finally:
        await db.execute(delete(Exercise).where(Exercise.id == exercise.id))
        await db.commit()
