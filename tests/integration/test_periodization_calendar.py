"""Календарь берёт фазу из снимка блока, а не из формулы по шаблону."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

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
from api.services.periodization import params
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


# --- Ревью Задачи 7 ----------------------------------------------------------


async def _seed_two_day_split(db, user_id: int) -> None:
    """Как _seed(), но сплит из ДВУХ разных тренировочных дней — нужен
    тесту на инвариант "счётчик дней сплита ведётся от block.start_date, а
    не от from_date": с одним днём в сплите подмена одной даты на другую
    ничего не меняет (slot всегда один и тот же)."""
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

    blueprint = SplitBlueprint(name="Тестовый сплит из двух дней", author_id=user_id, length_days=2, is_system=False)
    day_a = DayBlueprint(
        name="День A", author_id=user_id, template_type=DayTemplateType.FULL_BODY, is_system=False,
    )
    day_b = DayBlueprint(
        name="День B", author_id=user_id, template_type=DayTemplateType.FULL_BODY, is_system=False,
    )
    db.add_all([blueprint, day_a, day_b])
    await db.flush()
    db.add(DayMuscleTarget(day_id=day_a.id, muscle_group_id="full_body"))
    db.add(DayMuscleTarget(day_id=day_b.id, muscle_group_id="full_body"))
    db.add(SplitDaySlot(blueprint_id=blueprint.id, day_id=day_a.id, day_order=0))
    db.add(SplitDaySlot(blueprint_id=blueprint.id, day_id=day_b.id, day_order=1))
    db.add(
        UserSplit(
            app_user_id=user_id, blueprint_id=blueprint.id, is_active=True, current_day=1,
            selected_plans={},
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_split_counter_runs_from_block_start_not_from_date(db, test_user: AppUser):
    """Центральный инвариант задачи: счётчик отработанных дней сплита ведётся
    от block.start_date, а не от from_date. Три существующих теста этого файла
    во всех вызовах generate_block_days передают from_date == block.start_date,
    поэтому подмена одного на другое их не ловит — здесь from_date намеренно
    смещён на несколько дней вперёд относительно старта блока."""
    await _seed_two_day_split(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)

    offset = 3  # нечётный сдвиг — гарантированно попадает на другой слот из двух
    from_date = block.start_date + timedelta(days=offset)

    created = await SchedulingEngine.generate_block_days(
        db, test_user.id, block, from_date=from_date, until_date=from_date
    )
    assert created == 1

    day = (
        await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.target_date == from_date,
            )
        )
    ).scalar_one()

    # От начала блока к from_date прошло 3 дня без отдыха/блэкаутов, слот
    # сплита из двух элементов: (0 + 3) % 2 == 1 -> "День B". Если бы счётчик
    # считался от from_date (current_date = from_date вместо block.start_date),
    # первый же сгенерированный день получил бы слот с индексом 0 — "День A".
    assert day.day_tag == "День B", (
        "день сплита должен считаться от НАЧАЛА БЛОКА, а не от from_date"
    )


@pytest.mark.asyncio
async def test_launch_uses_block_snapshot_when_periodization_is_set(db, test_user: AppUser):
    """Находка 2 ревью Задачи 7: launch_and_unroll_plan обязан переключиться
    на снимок блока точно так же, как ensure_horizon/generate_block_days.
    Без фикса POST /splits/.../launch пересобирал бы календарь старым путём:
    фаза бралась бы из живого шаблона мезоцикла, а block_id не проставлялся
    бы вовсе."""
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    assert block.split_blueprint_id is not None

    await SchedulingEngine.launch_and_unroll_plan(
        db,
        test_user.id,
        block.split_blueprint_id,
        start_date=block.start_date,
        blackout_weekdays=[],
    )

    days = (
        await db.execute(
            select(UserCalendarDay)
            .where(UserCalendarDay.app_user_id == test_user.id)
            .order_by(UserCalendarDay.target_date)
        )
    ).scalars().all()

    assert days, "launch_and_unroll_plan должен создать дни календаря"
    assert all(d.block_id == block.id for d in days), "block_id обязан быть проставлен из снимка"
    assert days[0].meso_tag == "medium"
    assert days[7].meso_tag == "deload", "meso_tag должен браться из снимка блока (8-й день — вторая фаза)"


@pytest.mark.asyncio
async def test_completed_block_rolls_over_to_the_next(db, test_user: AppUser):
    """Находка 1 ревью Задачи 7: как только planned_end_date блока проходит,
    он закрывается с причиной completed, а следующий блок открывается по тому
    же шаблону с переносом entry_state = exit_state закрытого блока."""
    await _seed(db, test_user.id)
    start = date(2026, 7, 1)
    old_block = await ensure_active_block(db, test_user.id, start)
    old_block_id = old_block.id
    old_index = old_block.block_index
    old_planned_end = old_block.planned_end_date

    after_end = old_planned_end + timedelta(days=5)
    new_block = await ensure_active_block(db, test_user.id, after_end)

    closed = (
        await db.execute(select(TrainingBlock).where(TrainingBlock.id == old_block_id))
    ).scalar_one()

    assert closed.status == "closed"
    assert closed.close_reason == params.CLOSE_COMPLETED
    assert closed.actual_end_date == old_planned_end

    assert new_block.id != old_block_id
    assert new_block.status == "active"
    assert new_block.block_index == old_index + 1
    assert new_block.start_date == old_planned_end + timedelta(days=1), (
        "старт нового блока обязан быть planned_end_date + 1, а не today "
        "(иначе при заходе через несколько дней в календаре была бы дыра)"
    )
    assert new_block.entry_state == closed.exit_state


@pytest.mark.asyncio
async def test_active_block_is_not_rolled_over_early(db, test_user: AppUser):
    """Блок, у которого planned_end_date ещё впереди, остаётся активным и
    единственным — переход не должен срабатывать досрочно."""
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)

    still_before_end = block.planned_end_date - timedelta(days=1)
    result = await ensure_active_block(db, test_user.id, still_before_end)

    assert result.id == block.id
    assert result.status == "active"

    blocks = (
        await db.execute(
            select(TrainingBlock).where(TrainingBlock.app_user_id == test_user.id)
        )
    ).scalars().all()
    assert len(blocks) == 1


@pytest.mark.asyncio
async def test_horizon_stays_filled_across_the_block_boundary(db, test_user: AppUser):
    """САМЫЙ ВАЖНЫЙ тест Находки 1: прямая защита от найденного дефекта.
    Блок с прошедшим planned_end_date и календарь, заканчивающийся ровно на
    этой дате, — SchedulingEngine.ensure_horizon с today ПОСЛЕ границы обязан
    продолжить календарь за старую границу, а не оставить его пустым."""
    await _seed(db, test_user.id)
    start = date(2026, 7, 1)
    block = await ensure_active_block(db, test_user.id, start)
    boundary = block.planned_end_date

    await SchedulingEngine.generate_block_days(
        db, test_user.id, block, from_date=start, until_date=boundary
    )

    today = boundary + timedelta(days=10)
    await SchedulingEngine.ensure_horizon(db, test_user.id, today, horizon_days=90)

    days_after_boundary = (
        await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.target_date > boundary,
            )
        )
    ).scalars().all()

    assert days_after_boundary, (
        "календарь обязан продолжиться за старую границу блока — "
        "иначе пользователь, зашедший после конца блока, видит пустой календарь"
    )
