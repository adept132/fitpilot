"""Жизненный цикл блока: смена сплита и долгий перерыв (P0-08, Задача 14)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from api.services.day_template import DayTemplateType
from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    DayBlueprint,
    DayMuscleTarget,
    Exercise,
    Mesocycle,
    MesocyclePhase,
    PeriodizationProposal,
    SplitBlueprint,
    SplitDaySlot,
    TrainingBlock,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.periodization import params
from api.services.periodization.repository import ensure_active_block
from api.services.periodization.service import close_stale_block, refresh_proposals


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
async def test_layoff_closed_block_still_gets_its_summary_card(db, test_user: AppUser):
    """Ревью Задачи 14, Critical 1: блок с РЕАЛЬНОЙ завершённой тренировкой
    внутри, закрытый close_stale_block по правилу долгого перерыва, обязан
    получить карточку итогов на общих основаниях — ровно как и блок,
    закрытый обычным автопереходом.

    close_stale_block проверяет отсутствие сессий ПОСЛЕ planned_end_date
    блока (простаивал ли пользователь после конца блока), а НЕ отсутствие
    тренировок внутри самого блока — до фикса эта разница не учитывалась, и
    карточка итогов молча пропадала для любого блока, закрытого layoff'ом,
    даже честно отработанного."""
    block = await _seed(db, test_user.id, date(2026, 5, 1))  # покрывает 05-01..05-07

    marker = uuid.uuid4().hex[:8]
    exercise = Exercise(
        name=f"Тестовое упражнение layoff {marker}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(exercise)
    await db.flush()

    # Тренировка ВНУТРИ блока (05-03), а не после его конца (05-07) — именно
    # такую close_stale_block не видит своей проверкой "recent" и потому
    # блок всё равно закрывается как layoff.
    workout = WorkoutSession(
        app_user_id=test_user.id,
        source="free",
        status="finished",
        training_block_id=block.id,
        finished_at=datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
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
        result = await refresh_proposals(db, test_user.id, date(2026, 8, 3))

        await db.refresh(block)
        assert block.status == "closed"
        assert block.close_reason == params.CLOSE_LAYOFF

        boundary = [
            p for p in result
            if p.kind == params.KIND_BLOCK_BOUNDARY and p.block_id == block.id
        ]
        assert len(boundary) == 1, (
            "блок с реальной тренировкой внутри обязан получить карточку "
            "итогов, даже если закрыт как layoff, а не обычным автопереходом"
        )
    finally:
        # Порядок важен: подходы -> упражнения сессии -> сессия -> упражнение.
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
async def test_empty_layoff_closed_block_gets_no_card(db, test_user: AppUser):
    """Тот же сценарий, что и test_layoff_closed_block_still_gets_its_summary_card,
    но БЕЗ единой тренировки внутри блока: подводить нечего, карточки итогов
    быть не должно — то же правило, по которому промежуточные пустые блоки
    цепного автоперехода карточек не получают."""
    block = await _seed(db, test_user.id, date(2026, 5, 1))

    result = await refresh_proposals(db, test_user.id, date(2026, 8, 3))

    await db.refresh(block)
    assert block.status == "closed"
    assert block.close_reason == params.CLOSE_LAYOFF

    boundary = [p for p in result if p.kind == params.KIND_BLOCK_BOUNDARY]
    assert boundary == [], "пустой блок, закрытый layoff'ом, не должен получить карточку итогов"

    stored = (
        await db.execute(
            select(PeriodizationProposal).where(PeriodizationProposal.block_id == block.id)
        )
    ).scalars().all()
    assert stored == []


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
async def test_split_change_with_past_start_date_catches_up_to_today(
    client, db, test_user: AppUser
):
    """Ревью Задачи 14, Находка 2 (задокументировано, НЕ баг): запуск сплита с
    датой ЗАДНИМ ЧИСЛОМ создаёт блок, который сразу же оказывается просроченным,
    а launch_and_unroll_plan (через ensure_active_block) тут же цепным
    автопереходом докатывает его до сегодня, попутно закрывая пустые
    промежуточные блоки. Это честное следствие устройства блока
    фиксированной длины, а не дефект — тест закрепляет фактическое поведение:
    активный блок в итоге покрывает сегодняшний день, промежуточные блоки
    закрыты, данные не теряются (в этом сценарии их и не было — блоки пустые
    по построению)."""
    block = await _seed(db, test_user.id, date.today() - timedelta(days=100))
    blueprint = await _seed_split_blueprint(db, test_user.id)
    # Блок длиной 7 дней (один микроцикл): 20 дней в прошлом требуют ровно
    # двух перекатов автоперехода, чтобы догнать сегодня — далеко в пределах
    # params.MAX_CHAIN_ROLLOVERS, так что путь остаётся обычным (CLOSE_COMPLETED),
    # а не срывается в _close_for_layoff.
    past_start = date.today() - timedelta(days=20)

    response = await client.post(
        "/splits/launch",
        json={
            "blueprint_id": str(blueprint.id),
            "start_date": past_start.isoformat(),
            "blackout_weekdays": [],
        },
    )
    assert response.status_code == 200, response.text

    # /splits/launch отработал через СВОЁ (отдельное от db) соединение —
    # объект block, загруженный ещё в _seed, обязан обновиться явно, иначе
    # ORM отдаст устаревшие атрибуты из identity map этой сессии (тот же
    # паттерн db.refresh, что и в test_split_change_closes_active_block_...
    # выше).
    await db.refresh(block)
    all_blocks = (
        await db.execute(
            select(TrainingBlock)
            .where(TrainingBlock.app_user_id == test_user.id)
            .order_by(TrainingBlock.block_index)
        )
    ).scalars().all()

    active_blocks = [b for b in all_blocks if b.status == "active"]
    closed_blocks = [b for b in all_blocks if b.status == "closed"]
    assert len(active_blocks) == 1, "цепочка обязана сойтись ровно к одному активному блоку"
    active = active_blocks[0]
    assert active.start_date <= date.today() <= active.planned_end_date, (
        "активный блок обязан в итоге покрыть сегодняшний день"
    )

    # Первый закрытый блок в цепочке — тот, что закрыла смена сплита; все
    # следующие — пустые промежуточные блоки автоперехода.
    assert closed_blocks, "смена сплита обязана закрыть исходный блок"
    assert closed_blocks[0].id == block.id
    assert closed_blocks[0].close_reason == params.CLOSE_SPLIT_CHANGED
    assert all(b.close_reason == params.CLOSE_COMPLETED for b in closed_blocks[1:]), (
        "промежуточные блоки цепного автоперехода обязаны закрыться обычным "
        "переходом, а не layoff'ом — перерыв здесь короче MAX_CHAIN_ROLLOVERS"
    )

    # Данные не теряются: block_index идёт подряд без пропусков и коллизий.
    indices = [b.block_index for b in all_blocks]
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)


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
