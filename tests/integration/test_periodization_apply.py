"""Применение решений: правка снимка, перегенерация будущего, идемпотентность."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

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
    UserCalendarDay,
    UserSplit,
)
from api.services.periodization import params
from api.services.periodization.repository import ensure_active_block
from api.services.periodization.service import apply_decision
from api.services.scheduling_engine import SchedulingEngine

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


async def _seed_with_split(db, user_id: int) -> TrainingBlock:
    """Как _seed(), но с реальным активным сплитом (P0-08, Задача 10, ревью,
    Critical 1 и Critical 3).

    SchedulingEngine.generate_block_days без активного сплита выходит рано с
    created=0, НЕ доходя до своего внутреннего session.commit() (пустая
    slot_queue) — тестам на Critical 1 (дубликаты дней календаря при коллизии
    блоков) и Critical 3 (порядок коммитов) нужен по-настоящему рабочий
    генератор, иначе бы они либо не поймали настоящую коллизию дат, либо не
    увидели настоящий внутренний коммит, который и есть предмет проверки."""
    meso = Mesocycle(author_id=user_id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=3)
    db.add(meso)
    await db.flush()
    # Та же оговорка, что и в _seed(): третья фаза "hard", а не "deload" —
    # иначе insert_deload молча откажется вставлять разгрузку перед уже
    # существующей.
    for number, tier in enumerate(["easy", "medium", "hard"], start=1):
        db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=number, name=tier, effort_tier=tier))
    db.add(AppUserMesocycle(
        app_user_id=user_id, mesocycle_id=meso.id, is_active=True,
        microcycle_length=7, current_phase=1,
    ))
    db.add(AppUserMicrocycle(
        app_user_id=user_id, name="М", length_days=7,
        days_mapping={str(i): {"type": "hard", "tag": "full"} for i in range(1, 8)}, is_active=True,
    ))

    blueprint = SplitBlueprint(name="Сплит", author_id=user_id, length_days=1, is_system=False)
    day = DayBlueprint(
        name="Full Body", author_id=user_id, template_type=DayTemplateType.FULL_BODY, is_system=False,
    )
    db.add_all([blueprint, day])
    await db.flush()
    db.add(DayMuscleTarget(day_id=day.id, muscle_group_id="full_body"))
    db.add(SplitDaySlot(blueprint_id=blueprint.id, day_id=day.id, day_order=0))
    db.add(UserSplit(
        app_user_id=user_id, blueprint_id=blueprint.id, is_active=True, current_day=1, selected_plans={},
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


# --- Ревью Задачи 10: Critical 1/2/3 ------------------------------------------
#
# Общий корень всех трёх находок: PeriodizationProposal ссылается на блок, а
# пользователь может ответить на карточку сильно позже — к этому моменту
# автопереход (repository.roll_over_if_complete) уже закрыл тот блок и открыл
# следующий. apply_decision раньше смотрела ТОЛЬКО на proposal.status и
# никогда не проверяла состояние блока.


@pytest.mark.asyncio
async def test_stale_proposal_after_rollover_does_not_duplicate_calendar_days(db, test_user: AppUser):
    """Critical 1: устаревшее insert_deload против блока, который автопереход
    уже закрыл, раньше правило фазы ЗАКРЫТОГО блока и перегенерировало по нему
    дни календаря на даты, уже занятые днями НОВОГО активного блока — на
    выходе два UserCalendarDay на одну дату, GET /calendar/day падает с
    MultipleResultsFound. Теперь apply_decision обязана вернуть конфликт и не
    трогать ни фазы блока, ни календарь.

    Мутационная проверка (см. отчёт): без проверки block.status в
    apply_decision этот тест падает на последнем assert — insert_deload
    реально перегенерирует 6 дней блока (2026-08-25..2026-08-30) поверх уже
    существующих дней block2 на тех же датах.
    """
    block1 = await _seed_with_split(db, test_user.id)
    proposal = await _proposal(db, test_user.id, block1.id, params.KIND_EARLY_DELOAD)
    original_phases = list(block1.phases)

    rollover_date = date(2026, 8, 25)
    block2 = await ensure_active_block(db, test_user.id, rollover_date)
    assert block2.id != block1.id, "автопереход обязан был закрыть block1 и открыть block2"
    assert block2.start_date == block1.planned_end_date + timedelta(days=1)

    created = await SchedulingEngine.generate_block_days(
        db, test_user.id, block2, from_date=block2.start_date, until_date=block2.planned_end_date
    )
    assert created > 0, "у нового активного блока обязан быть реальный календарь для проверки коллизии"

    # "today" — день, когда пользователь наконец отвечает на устаревшую
    # карточку. first_future (today+1) обязан попасть в диапазон, уже занятый
    # block2, — это буквально воспроизводит Critical 1 при отсутствии фикса.
    today = block2.start_date
    result = await apply_decision(db, test_user.id, proposal.id, "insert_deload", today=today)

    assert result["status"] == "conflict"
    assert result["reason"] == "block_closed"

    await db.refresh(proposal)
    assert proposal.status == params.STATUS_EXPIRED, "устаревшая карточка обязана перестать быть pending"

    await db.refresh(block1)
    assert list(block1.phases) == original_phases, "фазы уже закрытого блока не должны были поменяться"

    rows = (
        await db.execute(
            select(UserCalendarDay.target_date, func.count(UserCalendarDay.id))
            .where(UserCalendarDay.app_user_id == test_user.id)
            .group_by(UserCalendarDay.target_date)
        )
    ).all()
    assert rows, "календарь нового блока обязан существовать"
    assert all(count == 1 for _, count in rows), (
        "ни на одну дату не должно приходиться больше одного дня календаря — "
        "устаревшее предложение не должно было перегенерировать дни закрытого блока"
    )


@pytest.mark.asyncio
async def test_stale_close_block_returns_conflict_not_crash(db, test_user: AppUser):
    """Critical 2: то же устаревшее предложение, но действием close_block.
    close_and_advance пытается создать следующий блок с block_index, который
    уже занял блок автоперехода, и раньше падала необработанным IntegrityError
    от uq_training_blocks_user_index — 500 вместо конфликта. Теперь —
    контрактный ответ, без исключения.

    Мутационная проверка (см. отчёт): без проверки block.status этот тест
    падает — apply_decision поднимает IntegrityError вместо возврата словаря.
    """
    block1 = await _seed(db, test_user.id)
    proposal = await _proposal(db, test_user.id, block1.id, params.KIND_EARLY_DELOAD)

    rollover_date = block1.planned_end_date + timedelta(days=2)
    block2 = await ensure_active_block(db, test_user.id, rollover_date)
    assert block2.id != block1.id
    assert block2.block_index == block1.block_index + 1, (
        "индекс, который close_block по старому коду попытался бы занять повторно"
    )

    result = await apply_decision(db, test_user.id, proposal.id, "close_block", today=rollover_date)

    assert result["status"] == "conflict"
    assert result["reason"] == "block_closed"

    await db.refresh(proposal)
    assert proposal.status == params.STATUS_EXPIRED

    blocks = (
        await db.execute(select(TrainingBlock).where(TrainingBlock.app_user_id == test_user.id))
    ).scalars().all()
    assert len(blocks) == 2, "close_block не должен был создать третий блок поверх уже открытого автопереходом"


@pytest.mark.asyncio
async def test_decision_is_recorded_before_the_mutation_commits(db, test_user: AppUser, monkeypatch):
    """Critical 3: SchedulingEngine.generate_block_days коммитит сессию САМА —
    раньше, чем apply_decision доходит до своего финального commit() со
    статусом предложения. Правильный порядок (proposal.status/decided_action/
    client_uuid/decided_at выставлены ДО вызова _perform) гарантирует, что оба
    факта — "решение принято" и "блок изменён" — оседают ОДНИМ и тем же
    внутренним commit'ом.

    Ловим это напрямую: обрываем apply_decision ровно между внутренним
    commit'ом (второй по счёту вызов session.commit — он внутри
    generate_block_days) и финальным. Читаем результат ЧЕРЕЗ НОВУЮ сессию
    (SessionLocal), а не ту же самую db, которую только что заставили упасть, —
    это и честнее (настоящий обрыв процесса означает именно новое соединение
    при повторе), и надёжнее: переиспользование db сразу после смоделированного
    исключения из monkeypatched commit() иногда оставляет пул соединений в
    состоянии, которое ловит не связанная с проверкой ошибка (MissingGreenlet
    при health-check пинге на checkout) — так тест ловил бы не то, что должен.

    Мутационная проверка (см. отчёт): если вернуть проставление
    status/decided_action/client_uuid/decided_at ПОСЛЕ вызова _perform (как
    было в исходном коде), этот тест падает на assert
    "persisted_proposal.status != params.STATUS_PENDING" — внутренний коммит
    зафиксирует только правку блока, решение останется pending.
    """
    from app.database import SessionLocal

    block = await _seed_with_split(db, test_user.id)
    proposal = await _proposal(db, test_user.id, block.id, params.KIND_EARLY_DELOAD)

    real_commit = db.commit
    seen = {"n": 0}

    async def crash_on_second_commit():
        seen["n"] += 1
        if seen["n"] == 2:
            raise RuntimeError("симулированный обрыв процесса между двумя commit'ами")
        await real_commit()

    monkeypatch.setattr(db, "commit", crash_on_second_commit)

    with pytest.raises(RuntimeError):
        await apply_decision(db, test_user.id, proposal.id, "insert_deload", today=TODAY)

    monkeypatch.undo()

    async with SessionLocal() as fresh:
        persisted_proposal = (
            await fresh.execute(select(PeriodizationProposal).where(PeriodizationProposal.id == proposal.id))
        ).scalar_one()
        persisted_block = (
            await fresh.execute(select(TrainingBlock).where(TrainingBlock.id == block.id))
        ).scalar_one()

        assert "deload" in [p["effort_tier"] for p in persisted_block.phases], (
            "внутренний commit generate_block_days обязан был зафиксировать правку фаз блока — "
            "иначе тест ничего не проверяет (мутация без побочного эффекта)"
        )
        assert persisted_proposal.status != params.STATUS_PENDING, (
            "решение обязано было зафиксироваться ТЕМ ЖЕ внутренним commit'ом, что и мутация блока"
        )
        assert persisted_proposal.status == params.STATUS_ACCEPTED
        assert persisted_proposal.decided_action == "insert_deload"

    # Повтор с ДРУГИМ client_uuid, уже обычной сессией теста, обязан получить
    # конфликт, а не второе применение — предложение уже не pending.
    repeat = await apply_decision(
        db, test_user.id, proposal.id, "insert_deload", client_uuid=uuid.uuid4().hex, today=TODAY
    )
    assert repeat["status"] == "conflict"
    block_after_repeat = (
        await db.execute(select(TrainingBlock).where(TrainingBlock.id == block.id))
    ).scalar_one()
    assert [p["effort_tier"] for p in block_after_repeat.phases].count("deload") == 1, (
        "повтор не должен был вставить вторую разгрузку"
    )


@pytest.mark.asyncio
async def test_postpone_applied_twice_does_not_shift_the_deload_twice(db, test_user: AppUser):
    """Critical 3, дополнительная проверка для postpone: phases.postpone_deload
    не защищена от повторного применения сама по себе — защититься от двойного
    сдвига обязан apply_decision, отказывая второму применению того же
    предложения (proposal.status уже не pending после первого)."""
    meso = Mesocycle(author_id=test_user.id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=2)
    db.add(meso)
    await db.flush()
    for number, tier in enumerate(["medium", "deload"], start=1):
        db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=number, name=tier, effort_tier=tier))
    db.add(AppUserMesocycle(
        app_user_id=test_user.id, mesocycle_id=meso.id, is_active=True, microcycle_length=7, current_phase=1,
    ))
    db.add(AppUserMicrocycle(
        app_user_id=test_user.id, name="М", length_days=7,
        days_mapping={"1": {"type": "hard", "tag": "full"}}, is_active=True,
    ))
    await db.commit()
    block = await ensure_active_block(db, test_user.id, BLOCK_START)
    proposal = await _proposal(db, test_user.id, block.id, params.KIND_POSTPONE_DELOAD)

    # "medium" — фаза ПЕРЕД разгрузкой, именно её продлевает postpone_deload
    # (сама разгрузка не переименовывается и не удлиняется, см. phases.postpone_deload).
    original_medium_length = next(p["length_days"] for p in block.phases if p["effort_tier"] == "medium")

    first = await apply_decision(db, test_user.id, proposal.id, "postpone", today=TODAY)
    assert first["status"] == "applied"
    await db.refresh(block)
    once_shifted_medium_length = next(p["length_days"] for p in block.phases if p["effort_tier"] == "medium")
    assert once_shifted_medium_length == original_medium_length + block.microcycle_length

    second = await apply_decision(db, test_user.id, proposal.id, "postpone", today=TODAY)
    assert second["status"] == "conflict", "предложение уже решено — второе применение обязано отказать"

    await db.refresh(block)
    twice_shifted_medium_length = next(p["length_days"] for p in block.phases if p["effort_tier"] == "medium")
    assert twice_shifted_medium_length == once_shifted_medium_length, (
        "повторное применение того же предложения не должно было сдвинуть разгрузку ещё раз"
    )
