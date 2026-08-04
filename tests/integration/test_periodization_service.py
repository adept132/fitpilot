"""Пересчёт предложений: материализация, дедупликация, деградация.

P0-08, Задача 9. Три поправки к брифу проверяются отдельными тестами:
- Поправка 1: карточка итогов блока (block_boundary) обязана материализоваться
  по ЗАКРЫТОМУ блоку, а не по новому активному, который занял его место.
- Поправка 4: `_chronic_level` обязан попадать в entry_state обоих путей
  создания блока (первый блок и автопереход).
- Стоимость усталостного сигнала: ранний выход из цикла compute_readiness
  должен реально работать (не более одного вызова, когда первый же день не
  fatigued; ровно FATIGUED_DAYS_FOR_DELOAD вызовов, когда все дни fatigued).
"""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

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
)
from api.services.periodization import params
from api.services.periodization.service import refresh_proposals

TODAY = date(2026, 8, 3)


async def _seed(db, user_id: int) -> None:
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


@pytest.mark.asyncio
async def test_cold_start_produces_no_proposals(db, test_user: AppUser):
    await _seed(db, test_user.id)
    assert await refresh_proposals(db, test_user.id, TODAY) == []


@pytest.mark.asyncio
async def test_completed_block_materializes_boundary_proposal(db, test_user: AppUser):
    await _seed(db, test_user.id)
    # Блок из двух недельных фаз, начатый месяц назад, — уже завершён.
    from api.services.periodization.repository import ensure_active_block

    block = await ensure_active_block(db, test_user.id, date(2026, 7, 1))
    result = await refresh_proposals(db, test_user.id, TODAY)

    assert [p.kind for p in result] == [params.KIND_BLOCK_BOUNDARY]
    stored = (
        await db.execute(
            select(PeriodizationProposal).where(PeriodizationProposal.block_id == block.id)
        )
    ).scalars().all()
    assert len(stored) == 1
    assert stored[0].status == params.STATUS_PENDING


@pytest.mark.asyncio
async def test_refresh_does_not_duplicate_pending_proposals(db, test_user: AppUser):
    await _seed(db, test_user.id)
    from api.services.periodization.repository import ensure_active_block

    block = await ensure_active_block(db, test_user.id, date(2026, 7, 1))
    await refresh_proposals(db, test_user.id, TODAY)
    await refresh_proposals(db, test_user.id, TODAY)

    stored = (
        await db.execute(
            select(PeriodizationProposal).where(PeriodizationProposal.block_id == block.id)
        )
    ).scalars().all()
    assert len(stored) == 1, "повторный пересчёт не должен плодить дубли"


@pytest.mark.asyncio
async def test_no_periodization_degrades_to_empty(db, test_user: AppUser):
    """Нет мезоцикла — нет блока и нет предложений, но и падения нет."""
    assert await refresh_proposals(db, test_user.id, TODAY) == []


# --- Поправка 1: карточка итогов принадлежит ЗАКРЫТОМУ блоку -----------------


@pytest.mark.asyncio
async def test_boundary_proposal_belongs_to_the_closed_block_not_the_new_active_one(
    db, test_user: AppUser
):
    """ensure_active_block сама перекатывает истёкший блок внутри себя — если
    бы refresh_proposals вызывала только её, закрытие осталось бы незамеченным
    и карточка итогов никогда бы не появилась (см. бриф, Поправка 1).

    Мутационная проверка (описана в отчёте): если убрать материализацию
    предложений по закрытому блоку из refresh_proposals и оставить только
    обычный путь по активному блоку, этот тест обязан упасть, потому что
    activный (новый) блок никогда не бывает is_complete сразу после создания.
    """
    await _seed(db, test_user.id)
    from api.services.periodization.repository import ensure_active_block

    old_block = await ensure_active_block(db, test_user.id, date(2026, 7, 1))
    result = await refresh_proposals(db, test_user.id, TODAY)

    boundary = [p for p in result if p.kind == params.KIND_BLOCK_BOUNDARY]
    assert len(boundary) == 1
    assert boundary[0].block_id == old_block.id

    active_blocks = (
        await db.execute(
            select(TrainingBlock).where(
                TrainingBlock.app_user_id == test_user.id,
                TrainingBlock.status == "active",
            )
        )
    ).scalars().all()
    assert len(active_blocks) == 1
    active_block = active_blocks[0]
    assert active_block.id != old_block.id, "блок обязан был перекатиться дальше old_block"

    # На новом активном блоке карточки итогов быть не может — она принадлежит
    # только что закрытому блоку.
    on_active = (
        await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.block_id == active_block.id,
                PeriodizationProposal.kind == params.KIND_BLOCK_BOUNDARY,
            )
        )
    ).scalars().all()
    assert on_active == []


# --- Поправка 4: _chronic_level в entry_state ---------------------------------


@pytest.mark.asyncio
async def test_first_block_entry_state_carries_chronic_level_marker(db, test_user: AppUser):
    await _seed(db, test_user.id)
    from api.services.periodization.repository import ensure_active_block

    block = await ensure_active_block(db, test_user.id, date(2026, 7, 1))

    assert block.entry_state is not None
    assert "_chronic_level" in block.entry_state, (
        "служебный ключ _chronic_level обязан попасть в entry_state первого "
        "блока — иначе перенос плановой разгрузки (_postpone) никогда не "
        "сработает: chronic_at_block_start всегда None"
    )


@pytest.mark.asyncio
async def test_rollover_entry_state_carries_chronic_level_marker(db, test_user: AppUser):
    await _seed(db, test_user.id)
    from api.services.periodization.repository import ensure_active_block

    await ensure_active_block(db, test_user.id, date(2026, 7, 1))
    active = await ensure_active_block(db, test_user.id, TODAY)

    assert active.entry_state is not None
    assert "_chronic_level" in active.entry_state, (
        "автопереход обязан заполнять _chronic_level так же, как и создание "
        "первого блока"
    )

    closed_blocks = (
        await db.execute(
            select(TrainingBlock).where(
                TrainingBlock.app_user_id == test_user.id,
                TrainingBlock.status == "closed",
            )
        )
    ).scalars().all()
    assert closed_blocks
    for closed in closed_blocks:
        assert closed.exit_state is not None
        assert "_chronic_level" in closed.exit_state


# --- Стоимость усталостного сигнала: ранний выход из цикла -------------------


@pytest.mark.asyncio
async def test_fatigue_loop_stops_on_first_non_fatigued_day(
    db, test_user: AppUser, monkeypatch
):
    await _seed(db, test_user.id)
    from api.services.periodization.repository import collect_decision_input, ensure_active_block

    block = await ensure_active_block(db, test_user.id, date(2026, 7, 1))

    calls = {"n": 0}

    async def fake_compute_readiness(session, app_user_id, now=None, p=None):
        calls["n"] += 1
        return SimpleNamespace(
            systemic=SimpleNamespace(band="fresh"),
            progression=SimpleNamespace(chronic_level=10.0, flag="ok"),
        )

    monkeypatch.setattr(
        "api.services.fatigue.service.compute_readiness", fake_compute_readiness
    )

    await collect_decision_input(db, test_user.id, block, TODAY)

    assert calls["n"] == 1, (
        "первый же не-fatigued день обязан остановить цикл — у большинства "
        "пользователей это ровно один вызов compute_readiness"
    )


@pytest.mark.asyncio
async def test_fatigue_loop_runs_full_window_when_always_fatigued(
    db, test_user: AppUser, monkeypatch
):
    await _seed(db, test_user.id)
    from api.services.periodization.repository import collect_decision_input, ensure_active_block

    block = await ensure_active_block(db, test_user.id, date(2026, 7, 1))

    calls = {"n": 0}

    async def fake_compute_readiness(session, app_user_id, now=None, p=None):
        calls["n"] += 1
        return SimpleNamespace(
            systemic=SimpleNamespace(band="fatigued"),
            progression=SimpleNamespace(chronic_level=10.0, flag="ok"),
        )

    monkeypatch.setattr(
        "api.services.fatigue.service.compute_readiness", fake_compute_readiness
    )

    await collect_decision_input(db, test_user.id, block, TODAY)

    assert calls["n"] == params.FATIGUED_DAYS_FOR_DELOAD, (
        "все дни в яме — цикл обязан дойти до конца окна, не больше и не меньше"
    )
