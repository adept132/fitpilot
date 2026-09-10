"""Переключатель фазы двигает блок, а не второй источник правды."""
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
    UserCalendarDay,
    UserSplit,
)
from api.services.periodization.repository import ensure_active_block
from api.services.scheduling_engine import SchedulingEngine


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


async def _seed_with_split(db, user_id: int):
    """Как _seed(), но с активным сплитом из одного дня — нужен тестам на
    перегенерацию календаря: без активного сплита слот-очередь
    generate_block_days пуста, и материализовать сегодняшний день нечем."""
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

    blueprint = SplitBlueprint(name="Сплит для теста", author_id=user_id, length_days=1, is_system=False)
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


# --- P0-08, Задача 13, ревью (Critical 1+2) ---------------------------------


@pytest.mark.asyncio
async def test_context_phase_fields_follow_the_block_after_switch(client, db, test_user: AppUser):
    """Critical 1: AppUserMesocycle.current_phase больше не источник правды.

    До фикса build_context брала selected_periodization_week/
    selected_periodization_phase_name из current_phase, который переключатель
    больше не обновляет, — один и тот же ответ показывал новую фазу в
    active_block.phase_number и замёрзшую старую в этих двух полях.

    Зеркало current_phase (Critical 3 этого же ревью) синхронизирует поле
    ТОЛЬКО в момент самого переключения — если после этого блок уйдёт дальше
    (естественный ход календаря, decide()/apply_decision), а current_phase
    больше никто не тронет, оно снова застынет. Именно поэтому тест не
    ограничивается одним переключением, а имитирует уход блока дальше НАПРЯМУЮ
    в БД (в обход эндпоинта) — так тест бьёт по чтению из current_phase, а не
    маскируется зеркалом, которое актуально только в момент самого свитча.
    """
    block = await _seed(db, test_user.id)

    switch = await client.post("/workout-center/active-mesocycle/phase", json={"phase": 1})
    assert switch.status_code == 200

    # Блок уходит дальше без участия эндпоинта (естественный ход календаря) —
    # current_phase (зеркало) остаётся на значении момента переключения (1),
    # а координата блока обязана уйти в фазу 3 (третья фаза начинается на
    # 15-й день, сдвиг на 14 дней назад — как и в test_switching_phase_moves_the_block_start).
    await db.refresh(block)
    block.start_date = block.start_date - timedelta(days=14)
    block.planned_end_date = block.planned_end_date - timedelta(days=14)
    await db.commit()

    response = await client.get("/workout-center/context")
    assert response.status_code == 200
    body = response.json()

    assert body["active_block"]["phase_number"] == 3
    assert body["selected_periodization_week"] == 3
    assert body["selected_periodization_phase_name"] == "deload"


@pytest.mark.asyncio
async def test_free_workout_after_switch_gets_the_new_phase(client, db, test_user: AppUser):
    """Critical 1: свободная тренировка после переключения фазы обязана
    получить НОВУЮ фазу в WorkoutSession.mesocycle_phase, а не старую из
    current_phase — иначе её training_block_id указывал бы на блок с одной
    координатой, а mesocycle_phase нёс бы другую.

    Как и в тесте контекста выше, блок уходит дальше НАПРЯМУЮ в БД уже после
    переключения — иначе зеркало current_phase (Critical 3) само по себе
    держало бы поля согласованными в этом единственном сценарии, и тест не
    отличил бы починку build_context/start_workout от одного только зеркала.
    """
    block = await _seed(db, test_user.id)

    switch_response = await client.post("/workout-center/active-mesocycle/phase", json={"phase": 1})
    assert switch_response.status_code == 200

    await db.refresh(block)
    block.start_date = block.start_date - timedelta(days=14)
    block.planned_end_date = block.planned_end_date - timedelta(days=14)
    await db.commit()

    start_response = await client.post("/workouts/start", json={"source": "free"})
    assert start_response.status_code == 200
    workout_id = start_response.json()["id"]

    from api.services.models import WorkoutSession

    workout = (
        await db.execute(select(WorkoutSession).where(WorkoutSession.id == workout_id))
    ).scalar_one()
    assert workout.mesocycle_phase == 3


@pytest.mark.asyncio
async def test_switch_rewrites_todays_calendar_day(client, db, test_user: AppUser):
    """Critical 2: переключатель прямо обещает «сегодня становится первым
    днём выбранной фазы». Если день на сегодня уже материализован (обычный
    случай для блока, идущего не первый день), он обязан быть переписан —
    иначе /calendar/day/{сегодня} и контекст workout-центра расходятся.
    Вчерашний день при этом остаётся неприкосновенным."""
    today = date.today()
    block = await _seed_with_split(db, test_user.id)

    # Материализуем сегодняшний день ДО переключения — обычный случай для
    # блока, идущего не первый день.
    created = await SchedulingEngine.generate_block_days(
        db, test_user.id, block, from_date=today, until_date=today
    )
    assert created == 1

    yesterday = UserCalendarDay(
        app_user_id=test_user.id, target_date=today - timedelta(days=1),
        day_tag="старый день", meso_tag="failure", micro_tag="hard",
        mesocycle_phase_number=99,
        is_rest_day=False, is_blackout=False, status="planned",
    )
    db.add(yesterday)
    await db.commit()

    response = await client.post("/workout-center/active-mesocycle/phase", json={"phase": 3})
    assert response.status_code == 200

    today_row = (
        await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.target_date == today,
            )
        )
    ).scalar_one()
    assert today_row.mesocycle_phase_number == 3
    assert today_row.meso_tag == "deload"

    await db.refresh(yesterday)
    assert yesterday.meso_tag == "failure"
    assert yesterday.mesocycle_phase_number == 99


@pytest.mark.asyncio
async def test_double_switch_to_same_phase_is_idempotent(client, db, test_user: AppUser):
    """Minor из ревью: двойное нажатие на одну и ту же фазу (двойной тап,
    повтор запроса) не имеет права сдвинуть блок ещё раз."""
    block = await _seed(db, test_user.id)

    first = await client.post("/workout-center/active-mesocycle/phase", json={"phase": 3})
    assert first.status_code == 200
    await db.refresh(block)
    start_after_first = block.start_date
    planned_end_after_first = block.planned_end_date

    second = await client.post("/workout-center/active-mesocycle/phase", json={"phase": 3})
    assert second.status_code == 200
    await db.refresh(block)

    assert block.start_date == start_after_first
    assert block.planned_end_date == planned_end_after_first

    second_body = second.json()
    assert second_body["active_block"]["phase_number"] == 3
    assert second_body["selected_periodization_week"] == 3
    assert second_body["selected_periodization_phase_name"] == "deload"
