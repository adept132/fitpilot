"""Пересчёт предложений: материализация, дедупликация, деградация.

P0-08, Задача 9. Три поправки к брифу проверяются отдельными тестами:
- Поправка 1: карточка итогов блока (block_boundary) обязана материализоваться
  по ЗАКРЫТОМУ блоку, а не по новому активному, который занял его место.
- Поправка 4: `_chronic_level` обязан попадать в entry_state обоих путей
  создания блока (первый блок и автопереход).
- Стоимость усталостного сигнала: ранний выход из цикла compute_readiness
  должен реально работать (не более одного вызова, когда первый же день не
  fatigued; ровно FATIGUED_DAYS_FOR_DELOAD вызовов, когда все дни fatigued).

Ревью Задачи 9 добавило ещё две находки со своими тестами:
- Находка 1: дедупликация early_deload/postpone_deload — по kind, БЕЗ
  reason_code, иначе смена повода между пересчётами плодит вторую карточку.
- Находка 3: safe_refresh_proposals откатывает работу через SAVEPOINT, а не
  session.rollback() целиком — чужие незакоммиченные изменения в той же
  сессии обязаны пережить упавший пересчёт периодизации.
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

# P0-08, Задача 14: TODAY выше даёт блоку (2026-07-01..2026-07-14) 20 дней
# просрочки без единой тренировки — с появлением close_stale_block это уже
# ЗАПАДАЕТ под params.LAYOFF_DAYS_AFTER_BLOCK_END (14 дней) и закрывается
# как layoff ДО того, как roll_over_if_complete успевает произвести карточку
# итогов (см. refresh_proposals: close_stale_block вызывается первой). Тесты
# ниже, проверяющие ИМЕННО карточку итогов обычного автоперехода, обязаны
# держаться в окне "уже просрочен, но меньше 14 дней без тренировок" — иначе
# они проверяли бы не автопереход, а другой (тоже правильный) путь закрытия.
MODERATE_OVERDUE = date(2026, 7, 16)


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
    # Блок из двух недельных фаз, начатый 2026-07-01, — уже завершён
    # (planned_end_date 2026-07-14), но просрочен меньше чем на
    # params.LAYOFF_DAYS_AFTER_BLOCK_END (см. MODERATE_OVERDUE выше) — это
    # обычный автопереход, а не layoff Задачи 14.
    from api.services.periodization.repository import ensure_active_block

    block = await ensure_active_block(db, test_user.id, date(2026, 7, 1))
    result = await refresh_proposals(db, test_user.id, MODERATE_OVERDUE)

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
    await refresh_proposals(db, test_user.id, MODERATE_OVERDUE)
    await refresh_proposals(db, test_user.id, MODERATE_OVERDUE)

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
    result = await refresh_proposals(db, test_user.id, MODERATE_OVERDUE)

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


# --- Ревью Задачи 9, Находка 1: одна pending-карточка early_deload на блок ----


@pytest.mark.asyncio
async def test_refresh_does_not_add_a_second_early_deload_with_another_reason(
    db, test_user: AppUser, monkeypatch
):
    """Пока пользователь не ответил на карточку досрочной разгрузки, повод
    между двумя пересчётами может смениться (сегодня высокая усталость, через
    неделю она прошла, зато набралось плато) — вторая карточка с другим
    поводом не должна появиться рядом с неотвеченной первой.

    decide() подменяется напрямую (а не собирается реальными физиологическими
    условиями через collect_decision_input) — так тест не зависит от того,
    каким именно триггером решатель добрался до early_deload, и проверяет
    ровно дедупликацию в _materialize, а не decide().
    """
    await _seed(db, test_user.id)
    from api.services.periodization.repository import ensure_active_block
    from api.services.periodization.types import Proposal

    moment = date(2026, 7, 10)  # внутри блока (2026-07-01..2026-07-14) — не закрыт
    block = await ensure_active_block(db, test_user.id, date(2026, 7, 1))
    assert moment <= block.planned_end_date, "тест держится на активном, не закрытом блоке"

    existing = PeriodizationProposal(
        app_user_id=test_user.id,
        block_id=block.id,
        kind=params.KIND_EARLY_DELOAD,
        reason_code=params.REASON_FATIGUE_HIGH,
        payload={"fatigued_days": 5, "after_phase_number": 1},
        status=params.STATUS_PENDING,
    )
    db.add(existing)
    await db.commit()

    monkeypatch.setattr(
        "api.services.periodization.service.decide",
        lambda inp: [
            Proposal(
                kind=params.KIND_EARLY_DELOAD,
                reason_code=params.REASON_BLOCK_PLATEAU,
                payload={"stalled": 2, "of": 3, "after_phase_number": 1},
            )
        ],
    )

    await refresh_proposals(db, test_user.id, moment)

    pending = (
        await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.block_id == block.id,
                PeriodizationProposal.kind == params.KIND_EARLY_DELOAD,
                PeriodizationProposal.status == params.STATUS_PENDING,
            )
        )
    ).scalars().all()
    assert len(pending) == 1, (
        "смена повода между пересчётами не должна плодить вторую карточку "
        "early_deload рядом с неотвеченной первой"
    )
    assert pending[0].reason_code == params.REASON_FATIGUE_HIGH, (
        "неотвеченная карточка должна остаться нетронутой, а не замениться "
        "новым поводом"
    )


# --- Ревью Задачи 9, Находка 3: safe_refresh_proposals не топит чужую работу -


@pytest.mark.asyncio
async def test_safe_refresh_keeps_caller_changes_on_failure(db, test_user: AppUser):
    """Голый session.rollback() в except откатывает ВСЮ транзакцию сессии, а
    не только то, что добавила периодизация — если вызывающий эндпоинт успел
    накопить в той же сессии собственные незакоммиченные изменения ДО вызова
    safe_refresh_proposals, упавший пересчёт периодизации утащил бы их за
    собой. SAVEPOINT (session.begin_nested()) обязан ограничить откат ровно
    работой периодизации."""
    from unittest.mock import patch

    from api.services.models import UserObservation
    from api.services.periodization.repository import ensure_active_block
    from api.services.periodization.service import safe_refresh_proposals

    await _seed(db, test_user.id)
    moment = date(2026, 7, 10)  # внутри блока — ни roll_over, ни ensure_active_block
    block = await ensure_active_block(db, test_user.id, date(2026, 7, 1))  # не коммитят по новой
    assert moment <= block.planned_end_date

    # Работа вызывающей стороны, накопленная в ЭТОЙ ЖЕ сессии ДО вызова
    # safe_refresh_proposals и ещё не закоммиченная — ровно сценарий Задач 11/13.
    observation = UserObservation(
        app_user_id=test_user.id,
        kind="morning_readiness",
        value=1.0,
    )
    db.add(observation)

    with patch(
        "api.services.periodization.service.collect_decision_input",
        side_effect=RuntimeError("boom"),
    ):
        result = await safe_refresh_proposals(db, test_user.id, moment)

    assert result == [], "упавший пересчёт обязан деградировать в пустой список"

    # Коммитит вызывающая сторона (эндпоинт) — как и в реальном сценарии.
    await db.commit()

    stored = (
        await db.execute(
            select(UserObservation).where(UserObservation.app_user_id == test_user.id)
        )
    ).scalars().all()
    assert len(stored) == 1, (
        "запись вызывающей стороны обязана пережить упавший пересчёт "
        "периодизации и закоммититься вместе с остальной работой эндпоинта"
    )


# --- Финальное ревью, Находка 2: висящие карточки итогов истекают -----------


@pytest.mark.asyncio
async def test_older_boundary_proposals_expire_when_a_new_block_closes(
    db, test_user: AppUser
):
    """Пользователь ушёл с экрана итогов первого блока, не ответив на
    карточку (штатный выбор «доработать по плану», см. бриф Находки 2) — она
    осталась pending. Когда закрывается ВТОРОЙ блок и по нему материализуется
    своя карточка итогов, разбор первого блока уже неактуален — его место
    занял разбор второго. Без фикса первая карточка висела бы pending
    бессрочно и заслоняла бы вторую."""
    await _seed(db, test_user.id)
    from api.services.periodization.repository import ensure_active_block

    block1 = await ensure_active_block(db, test_user.id, date(2026, 7, 1))
    assert block1.planned_end_date == date(2026, 7, 14)

    # Закрываем первый блок автопереходом (тот же приём, что и MODERATE_OVERDUE
    # выше — просрочка меньше params.LAYOFF_DAYS_AFTER_BLOCK_END).
    first_result = await refresh_proposals(db, test_user.id, date(2026, 7, 16))
    assert [p.kind for p in first_result] == [params.KIND_BLOCK_BOUNDARY]

    block2 = (
        await db.execute(
            select(TrainingBlock).where(
                TrainingBlock.app_user_id == test_user.id,
                TrainingBlock.status == "active",
            )
        )
    ).scalars().first()
    assert block2 is not None and block2.id != block1.id
    assert block2.planned_end_date == date(2026, 7, 28)

    # Пользователь так и не ответил на карточку первого блока — она осталась
    # pending. Теперь закрываем ВТОРОЙ блок тем же автопереходом (2 дня
    # просрочки — тот же запас, что и у MODERATE_OVERDUE).
    second_result = await refresh_proposals(db, test_user.id, date(2026, 7, 30))
    assert [p.kind for p in second_result] == [params.KIND_BLOCK_BOUNDARY]

    proposal1 = (
        await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.block_id == block1.id,
                PeriodizationProposal.kind == params.KIND_BLOCK_BOUNDARY,
            )
        )
    ).scalars().first()
    proposal2 = (
        await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.block_id == block2.id,
                PeriodizationProposal.kind == params.KIND_BLOCK_BOUNDARY,
            )
        )
    ).scalars().first()

    assert proposal1 is not None and proposal1.status == params.STATUS_EXPIRED, (
        "карточка итогов первого (более раннего) блока обязана истечь, когда "
        "закрылся второй"
    )
    assert proposal2 is not None and proposal2.status == params.STATUS_PENDING, (
        "карточка итогов только что закрытого блока обязана остаться pending"
    )
