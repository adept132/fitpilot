"""HTTP-контракт периодизации."""
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
    PeriodizationProposal,
    SplitBlueprint,
    SplitDaySlot,
    TrainingBlock,
    UserSplit,
)
from api.services.periodization import params
from api.services.periodization.repository import ensure_active_block
from api.services.scheduling_engine import SchedulingEngine


async def _seed(db, user_id: int, start: date):
    meso = Mesocycle(author_id=user_id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=2)
    db.add(meso)
    await db.flush()
    for number, tier in enumerate(["medium", "deload"], start=1):
        db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=number, name=tier, effort_tier=tier))
    db.add(AppUserMesocycle(
        app_user_id=user_id, mesocycle_id=meso.id, is_active=True,
        microcycle_length=7, current_phase=1,
    ))
    db.add(AppUserMicrocycle(
        app_user_id=user_id, name="М", length_days=7,
        days_mapping={"1": {"type": "hard", "tag": "full"}}, is_active=True,
    ))
    await db.commit()
    return await ensure_active_block(db, user_id, start)


@pytest.mark.asyncio
async def test_context_returns_coordinates(client, db, test_user: AppUser):
    await _seed(db, test_user.id, date.today())

    response = await client.get("/periodization/context")

    assert response.status_code == 200
    body = response.json()
    assert body["block"]["block_index"] == 1
    assert body["block"]["phase_ordinal"] == 1
    assert body["block"]["phases_total"] == 2
    assert body["block"]["effort_tier"] == "medium"
    assert body["proposals"] == []


@pytest.mark.asyncio
async def test_context_without_periodization_is_empty_not_an_error(client, test_user: AppUser):
    response = await client.get("/periodization/context")
    assert response.status_code == 200
    assert response.json() == {"block": None, "proposals": []}


@pytest.mark.asyncio
async def test_decision_endpoint_applies_and_repeats_safely(client, db, test_user: AppUser):
    block = await _seed(db, test_user.id, date.today())
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block.id,
        kind=params.KIND_EARLY_DELOAD, reason_code=params.REASON_FATIGUE_HIGH,
        payload={"after_phase_number": 1}, status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()
    marker = uuid.uuid4().hex

    first = await client.post(
        f"/periodization/proposals/{proposal.id}/decision",
        json={"action": "insert_deload", "client_uuid": marker},
    )
    second = await client.post(
        f"/periodization/proposals/{proposal.id}/decision",
        json={"action": "insert_deload", "client_uuid": marker},
    )

    assert first.status_code == 200
    assert first.json()["status"] == "applied"
    assert second.status_code == 200
    assert second.json()["status"] == "already_applied"


@pytest.mark.asyncio
async def test_conflicting_decision_returns_409(client, db, test_user: AppUser):
    block = await _seed(db, test_user.id, date.today())
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block.id,
        kind=params.KIND_EARLY_DELOAD, reason_code=params.REASON_FATIGUE_HIGH,
        payload={"after_phase_number": 1}, status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    await client.post(
        f"/periodization/proposals/{proposal.id}/decision",
        json={"action": "insert_deload", "client_uuid": uuid.uuid4().hex},
    )
    conflict = await client.post(
        f"/periodization/proposals/{proposal.id}/decision",
        json={"action": "decline", "client_uuid": uuid.uuid4().hex},
    )

    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_summary_reports_entry_and_exit(client, db, test_user: AppUser):
    block = await _seed(db, test_user.id, date.today())

    response = await client.get(f"/periodization/blocks/{block.id}/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["block_index"] == 1
    assert body["close_reason"] is None
    assert body["exercises"] == []


@pytest.mark.asyncio
async def test_summary_of_a_foreign_block_is_404(client, db, test_user: AppUser):
    response = await client.get("/periodization/blocks/999999/summary")
    assert response.status_code == 404


# --- Поправки к брифу Задачи 11 ------------------------------------------


async def _seed_with_split(db, user_id: int, start: date):
    """Как _seed(), но добавляет минимальный активный сплит из одного
    тренировочного (не-отдыхового) дня — нужен SchedulingEngine.generate_block_days,
    чтобы в календаре реально появились дни (поправка 2: workouts_to_deload
    считается по настоящим UserCalendarDay, а без сплита слот-очередь пуста и
    generate_block_days создаёт ноль строк)."""
    meso = Mesocycle(author_id=user_id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=2)
    db.add(meso)
    await db.flush()
    for number, tier in enumerate(["medium", "deload"], start=1):
        db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=number, name=tier, effort_tier=tier))
    db.add(AppUserMesocycle(
        app_user_id=user_id, mesocycle_id=meso.id, is_active=True,
        microcycle_length=7, current_phase=1,
    ))
    db.add(AppUserMicrocycle(
        app_user_id=user_id, name="М", length_days=7,
        days_mapping={str(i): {"type": "hard", "tag": "full"} for i in range(1, 8)},
        is_active=True,
    ))

    blueprint = SplitBlueprint(name="Тестовый сплит", author_id=user_id, length_days=1, is_system=False)
    day = DayBlueprint(
        name="Full Body", author_id=user_id, template_type=DayTemplateType.FULL_BODY, is_system=False,
    )
    db.add_all([blueprint, day])
    await db.flush()
    db.add(DayMuscleTarget(day_id=day.id, muscle_group_id="full_body"))
    db.add(SplitDaySlot(blueprint_id=blueprint.id, day_id=day.id, day_order=0))
    db.add(UserSplit(
        app_user_id=user_id, blueprint_id=blueprint.id, is_active=True, current_day=1,
        selected_plans={},
    ))
    await db.commit()
    return await ensure_active_block(db, user_id, start)


@pytest.mark.asyncio
async def test_context_reports_real_workouts_to_deload(client, db, test_user: AppUser):
    """Поправка 2 брифа: workouts_to_deload не заглушка, а настоящий подсчёт
    не-выходных/не-заблокированных дней календаря до старта ближайшей
    разгрузки — той же логикой, что и repository.collect_decision_input."""
    today = date.today()
    block = await _seed_with_split(db, test_user.id, today)
    created = await SchedulingEngine.generate_block_days(
        db, test_user.id, block, from_date=today, until_date=block.planned_end_date
    )
    assert created > 0, "нужен реальный календарь, иначе тест ничего не проверяет"
    await db.commit()

    response = await client.get("/periodization/context")

    assert response.status_code == 200
    body = response.json()
    # Первая фаза — 7 дней "medium", вторая — "deload": разгрузка начинается
    # через 7 дней от старта блока (сегодня — 1-й день блока).
    assert body["block"]["days_to_deload"] == 7
    assert body["block"]["workouts_to_deload"] == 7, (
        "сплит из одного полнотельного дня без блэкаутов — все 7 дней "
        "до разгрузки рабочие, ни один не выходной и не заблокирован"
    )


@pytest.mark.asyncio
async def test_context_workouts_to_deload_is_null_without_upcoming_deload(client, db, test_user: AppUser):
    """Симметричный случай: если разгрузки впереди нет (days_to_deload is
    None), workouts_to_deload обязан быть null, а не 0 или заглушкой."""
    meso = Mesocycle(author_id=test_user.id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=1)
    db.add(meso)
    await db.flush()
    db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=1, name="medium", effort_tier="medium"))
    db.add(AppUserMesocycle(
        app_user_id=test_user.id, mesocycle_id=meso.id, is_active=True,
        microcycle_length=7, current_phase=1,
    ))
    db.add(AppUserMicrocycle(
        app_user_id=test_user.id, name="М", length_days=7,
        days_mapping={"1": {"type": "hard", "tag": "full"}}, is_active=True,
    ))
    await db.commit()
    await ensure_active_block(db, test_user.id, date.today())

    response = await client.get("/periodization/context")

    assert response.status_code == 200
    body = response.json()
    assert body["block"]["days_to_deload"] is None
    assert body["block"]["workouts_to_deload"] is None


@pytest.mark.asyncio
async def test_decision_on_proposal_from_closed_block_returns_409_block_closed(
    client, db, test_user: AppUser
):
    """Поправка 1 брифа: apply_decision тоже отвечает конфликтом, когда
    блок-мутирующее действие (insert_deload/close_block/postpone) приходит
    по предложению от блока, который автопереход уже закрыл, пока
    пользователь не отвечал на карточку. Этот случай отличается от "чужого
    решения по уже решённому предложению" (proposal.status уже не pending) —
    здесь proposal ВСЁ ЕЩЁ pending, а закрылся именно блок. Тело ответа
    обязано нести reason=block_closed, чтобы клиент отличал этот случай от
    обычного конфликта решений."""
    start = date.today() - timedelta(days=20)
    block1 = await _seed(db, test_user.id, start)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block1.id,
        kind=params.KIND_EARLY_DELOAD, reason_code=params.REASON_FATIGUE_HIGH,
        payload={"after_phase_number": 1}, status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    # Автопереход: реальное "сегодня" уже позже planned_end_date block1
    # (2 фазы по 7 дней = 14 дней, начало 20 дней назад) — закрывает block1 и
    # открывает block2.
    block2 = await ensure_active_block(db, test_user.id, date.today())
    assert block2.id != block1.id, "переход обязан был случиться для этого теста"
    await db.commit()

    response = await client.post(
        f"/periodization/proposals/{proposal.id}/decision",
        json={"action": "insert_deload", "client_uuid": uuid.uuid4().hex},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == "conflict"
    assert detail["reason"] == "block_closed"


@pytest.mark.asyncio
async def test_summary_is_reachable_for_a_closed_block(client, db, test_user: AppUser):
    """Поправка 3 брифа: карточка итогов чаще всего звучит именно по блоку,
    который только что закрыл автопереход, — эндпоинт не должен требовать,
    чтобы блок был активным."""
    start = date.today() - timedelta(days=20)
    block1 = await _seed(db, test_user.id, start)
    block1_id = block1.id

    block2 = await ensure_active_block(db, test_user.id, date.today())
    assert block2.id != block1_id, "переход обязан был случиться для этого теста"
    await db.commit()

    closed = (
        await db.execute(select(TrainingBlock).where(TrainingBlock.id == block1_id))
    ).scalar_one()
    assert closed.status == "closed", "предпосылка теста: блок из запроса ниже обязан быть закрыт"

    response = await client.get(f"/periodization/blocks/{block1_id}/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "closed"
