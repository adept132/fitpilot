"""Применение решений: правка снимка, перегенерация будущего, идемпотентность."""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    Mesocycle,
    MesocyclePhase,
    PeriodizationProposal,
    TrainingBlock,
    UserCalendarDay,
)
from api.services.periodization import params
from api.services.periodization.repository import ensure_active_block
from api.services.periodization.service import apply_decision

TODAY = date(2026, 8, 17)
BLOCK_START = date(2026, 8, 3)


async def _seed(db, user_id: int):
    # ВНИМАНИЕ: третья фаза — "hard", а НЕ "deload". phase_ops.insert_deload
    # (Задача 9) отказывается вставлять разгрузку прямо перед уже
    # существующей разгрузкой (см. её защитную ветку и
    # tests/test_periodization_phases.py::test_insert_deload_before_existing_deload_changes_nothing)
    # — с шаблоном easy/medium/deload вставка после phase_number=2 была бы
    # молчаливым no-op, а не правкой снимка, которую эти тесты проверяют.
    meso = Mesocycle(author_id=user_id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=3)
    db.add(meso)
    await db.flush()
    for number, tier in enumerate(["easy", "medium", "hard"], start=1):
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
    return await ensure_active_block(db, user_id, BLOCK_START)


async def _proposal(db, user_id: int, block_id: int, kind: str) -> PeriodizationProposal:
    row = PeriodizationProposal(
        app_user_id=user_id, block_id=block_id, kind=kind,
        reason_code=params.REASON_FATIGUE_HIGH,
        payload={"after_phase_number": 2}, status=params.STATUS_PENDING,
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
async def test_insert_deload_edits_the_snapshot(db, test_user: AppUser):
    block = await _seed(db, test_user.id)
    proposal = await _proposal(db, test_user.id, block.id, params.KIND_EARLY_DELOAD)

    result = await apply_decision(db, test_user.id, proposal.id, "insert_deload")

    assert result["status"] == "applied"
    await db.refresh(block)
    tiers = [p["effort_tier"] for p in block.phases]
    assert tiers == ["easy", "medium", "deload", "hard"], "разгрузка вставлена сразу после фазы 2"
    assert block.status == "active", "блок продолжается — это выбор «доработать по плану»"


@pytest.mark.asyncio
async def test_close_block_ends_it_and_opens_the_next(db, test_user: AppUser):
    block = await _seed(db, test_user.id)
    proposal = await _proposal(db, test_user.id, block.id, params.KIND_EARLY_DELOAD)

    await apply_decision(db, test_user.id, proposal.id, "close_block")
    await db.refresh(block)

    assert block.status == "closed"
    assert block.close_reason == params.CLOSE_EARLY_DELOAD
    assert block.actual_end_date is not None
    assert block.exit_state is not None

    nxt = (
        await db.execute(
            select(TrainingBlock).where(
                TrainingBlock.app_user_id == test_user.id,
                TrainingBlock.status == "active",
            )
        )
    ).scalar_one()
    assert nxt.block_index == 2
    assert nxt.entry_state == block.exit_state, "перенос состояния — это одно и то же"


@pytest.mark.asyncio
async def test_close_block_wipes_stale_future_days_from_the_old_block(db, test_user: AppUser):
    """Досрочное закрытие обрывает блок ДО его planned_end_date — календарь
    мог уже быть заполнен наперёд вплоть до старой границы. Если не снести
    эти дни явно, они останутся привязаны к закрытому блоку и столкнутся по
    той же дате с днями, которые сгенерирует новый блок:
    GET /calendar/day делает select(...).scalar_one_or_none() по
    (app_user_id, target_date) и падает с MultipleResultsFound на дубликате.
    """
    block = await _seed(db, test_user.id)
    stale = UserCalendarDay(
        app_user_id=test_user.id, target_date=date(2026, 8, 20), block_id=block.id,
        day_tag="будущее", meso_tag="medium", micro_tag="hard",
        is_rest_day=False, is_blackout=False, status="planned",
    )
    db.add(stale)
    await db.commit()

    proposal = await _proposal(db, test_user.id, block.id, params.KIND_EARLY_DELOAD)
    await apply_decision(db, test_user.id, proposal.id, "close_block", today=TODAY)

    remaining = (
        await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.target_date == date(2026, 8, 20),
            )
        )
    ).scalars().all()
    assert remaining == [], "будущий день старого блока не должен пережить досрочное закрытие"


@pytest.mark.asyncio
async def test_start_next_block_only_fixes_the_decision(db, test_user: AppUser):
    """Поправка 1 брифа: переход между блоками теперь автоматический
    (repository.roll_over_if_complete, Задача 7) — к моменту, когда
    пользователь видит карточку итогов блока, блок уже закрыт автопереходом.
    start_next_block по предложению block_boundary поэтому больше не
    закрывает блок сам — только фиксирует, что пользователь увидел карточку.
    """
    block = await _seed(db, test_user.id)
    proposal = await _proposal(db, test_user.id, block.id, params.KIND_BLOCK_BOUNDARY)

    result = await apply_decision(db, test_user.id, proposal.id, "start_next_block")

    assert result["status"] == "applied"
    await db.refresh(block)
    await db.refresh(proposal)
    assert block.status == "active", "блок не должен закрываться этим действием — переход уже случился"
    assert proposal.status == params.STATUS_ACCEPTED
    assert proposal.decided_action == "start_next_block"

    count = len(
        (
            await db.execute(
                select(TrainingBlock).where(TrainingBlock.app_user_id == test_user.id)
            )
        ).scalars().all()
    )
    assert count == 1, "новый блок создаваться не должен — его уже создал автопереход"


@pytest.mark.asyncio
async def test_decline_marks_proposal_and_changes_nothing(db, test_user: AppUser):
    block = await _seed(db, test_user.id)
    before = list(block.phases)
    proposal = await _proposal(db, test_user.id, block.id, params.KIND_EARLY_DELOAD)

    await apply_decision(db, test_user.id, proposal.id, "decline")

    await db.refresh(block)
    await db.refresh(proposal)
    assert proposal.status == params.STATUS_DECLINED
    assert block.phases == before


@pytest.mark.asyncio
async def test_decision_is_idempotent_by_client_uuid(db, test_user: AppUser):
    block = await _seed(db, test_user.id)
    proposal = await _proposal(db, test_user.id, block.id, params.KIND_EARLY_DELOAD)
    marker = uuid.uuid4().hex

    first = await apply_decision(db, test_user.id, proposal.id, "insert_deload", client_uuid=marker)
    second = await apply_decision(db, test_user.id, proposal.id, "insert_deload", client_uuid=marker)

    assert first["status"] == "applied"
    assert second["status"] == "already_applied"
    await db.refresh(block)
    assert [p["effort_tier"] for p in block.phases].count("deload") == 1, "повтор не вставляет вторую разгрузку"


@pytest.mark.asyncio
async def test_foreign_decision_on_decided_proposal_conflicts(db, test_user: AppUser):
    block = await _seed(db, test_user.id)
    proposal = await _proposal(db, test_user.id, block.id, params.KIND_EARLY_DELOAD)

    await apply_decision(db, test_user.id, proposal.id, "insert_deload", client_uuid=uuid.uuid4().hex)
    result = await apply_decision(db, test_user.id, proposal.id, "decline", client_uuid=uuid.uuid4().hex)

    assert result["status"] == "conflict"


@pytest.mark.asyncio
async def test_regeneration_leaves_the_past_alone(db, test_user: AppUser):
    block = await _seed(db, test_user.id)
    old = UserCalendarDay(
        app_user_id=test_user.id, target_date=date(2026, 8, 10), block_id=block.id,
        day_tag="прошлое", meso_tag="easy", micro_tag="hard",
        is_rest_day=False, is_blackout=False, status="planned",
    )
    # Граница — СТРОГО после сегодня: день, датированный самим TODAY, тоже
    # не должен переписываться. Без этого дня тест не отличил бы "строго
    # после" от "начиная с сегодня" — оба варианта одинаково не трогают день
    # 8/10, который раньше TODAY при любом определении границы (проверено
    # мутационным прогоном: без этой строки тест ложно проходил и при
    # сдвинутой границе).
    today_row = UserCalendarDay(
        app_user_id=test_user.id, target_date=TODAY, block_id=block.id,
        day_tag="сегодня", meso_tag="easy", micro_tag="hard",
        is_rest_day=False, is_blackout=False, status="planned",
    )
    db.add(old)
    db.add(today_row)
    await db.commit()

    proposal = await _proposal(db, test_user.id, block.id, params.KIND_EARLY_DELOAD)
    await apply_decision(db, test_user.id, proposal.id, "insert_deload", today=TODAY)

    await db.refresh(old)
    await db.refresh(today_row)
    assert old.meso_tag == "easy", "день до сегодняшнего не переписывается"
    assert old.day_tag == "прошлое"
    assert today_row.meso_tag == "easy", "сегодняшний день тоже не переписывается — граница строго после"
    assert today_row.day_tag == "сегодня"
