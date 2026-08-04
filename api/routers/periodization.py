"""Ручки периодизации (P0-08, Задача 11)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.periodization import (
    BlockCoordinateRead,
    BlockExerciseSummary,
    BlockSummaryRead,
    DecisionRequest,
    PeriodizationContextRead,
    ProposalRead,
)
from api.services.app_user_service import get_current_app_user
from api.services.models import (
    AppUser,
    Exercise,
    PeriodizationProposal,
    TrainingBlock,
)
from api.services.periodization import params
from api.services.periodization.position import position
from api.services.periodization.repository import (
    block_state,
    count_workouts_to_deload,
    get_active_block,
)
from api.services.periodization.service import apply_decision, safe_refresh_proposals
from api.services.scheduling_engine import SchedulingEngine

router = APIRouter(prefix="/periodization", tags=["Periodization"])


def _proposal_out(row: PeriodizationProposal) -> ProposalRead:
    return ProposalRead(
        id=row.id,
        kind=row.kind,
        reason_code=row.reason_code,
        reason_text=params.REASON_TEXTS.get(row.reason_code, ""),
        payload=row.payload or {},
        status=row.status,
    )


@router.get("/context", response_model=PeriodizationContextRead)
async def get_periodization_context(
    local_date: date | None = None,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> PeriodizationContextRead:
    """Координата блока и висящие предложения. Пустой блок — не ошибка:
    пользователь без настроенной периодизации получает {block: None,
    proposals: []}, а не 404/500."""
    today = local_date or date.today()
    await safe_refresh_proposals(db, current_user.id, today)

    # Ревью, Находка 2: workouts_to_deload ниже считается по УЖЕ
    # СУЩЕСТВУЮЩИМ строкам UserCalendarDay — а достраивает календарь на
    # будущее только SchedulingEngine.ensure_horizon, которую до сих пор звал
    # только роутер календаря (api/routers/calendar.py). Если мобильный клиент
    # дёрнет этот эндпоинт раньше, чем /calendar/day (или горизонт короче, чем
    # расстояние до разгрузки), подсчёт ниже увидел бы пустой/недостроенный
    # хвост календаря и занизил бы число — в пределе до нуля, хотя разгрузка
    # реально впереди. Зовём ту же самую достройку, что и календарь, ДО
    # подсчёта — счётчик обязан опираться на достроенный горизонт, а не на
    # то, что случайно успел сгенерировать другой эндпоинт раньше.
    await SchedulingEngine.ensure_horizon(db, current_user.id, today)

    block = await get_active_block(db, current_user.id)
    if block is None:
        return PeriodizationContextRead(block=None, proposals=[])

    state = block_state(block)
    pos = position(state, today)
    current = next(
        (p for p in state.phases if p.phase_number == pos.phase_number), state.phases[-1]
    )

    pending = (
        await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.block_id == block.id,
                PeriodizationProposal.status == params.STATUS_PENDING,
            ).order_by(PeriodizationProposal.id)
        )
    ).scalars().all()

    # Поправка 2 брифа Задачи 11: workouts_to_deload — настоящий подсчёт по
    # repository.count_workouts_to_deload (та же функция, что зовёт
    # repository.collect_decision_input для решателя), а не заглушка None.
    workouts_to_deload = await count_workouts_to_deload(
        db, current_user.id, today, pos.days_to_deload
    )

    return PeriodizationContextRead(
        block=BlockCoordinateRead(
            block_id=block.id,
            block_index=block.block_index,
            phase_number=pos.phase_number,
            phase_name=current.name,
            effort_tier=pos.effort_tier,
            phase_ordinal=pos.phase_ordinal,
            phases_total=pos.phases_total,
            day_in_block=pos.day_in_block,
            days_to_deload=pos.days_to_deload,
            workouts_to_deload=workouts_to_deload,
            is_complete=pos.is_complete,
            start_date=block.start_date,
            planned_end_date=block.planned_end_date,
        ),
        proposals=[_proposal_out(row) for row in pending],
    )


@router.post("/proposals/{proposal_id}/decision")
async def decide_proposal(
    proposal_id: int,
    payload: DecisionRequest,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await apply_decision(
        db, current_user.id, proposal_id, payload.action, payload.client_uuid
    )
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Предложение не найдено")
    if result["status"] == "conflict":
        # Поправка 1 брифа Задачи 11: apply_decision (Задача 10) отдаёт
        # status=conflict в ДВУХ разных случаях — чужое решение по уже
        # решённому предложению (current_status/decided_action в теле) и
        # блок-мутирующее действие по предложению от уже закрытого блока
        # (reason=block_closed в теле). Оба превращаем в 409 одинаково —
        # result уже несёт то поле, которым клиент отличит один случай от
        # другого, отдельная ветка здесь не нужна.
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get("/blocks/{block_id}/summary", response_model=BlockSummaryRead)
async def get_block_summary(
    block_id: int,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> BlockSummaryRead:
    """Итоги блока — доступны для ЛЮБОГО статуса блока (поправка 3 брифа
    Задачи 11). Карточка итогов (Задача 9) материализуется по блоку, который
    только что закрыл автопереход, — эндпоинт чаще всего будут звать именно
    для закрытого блока, поэтому здесь сознательно нет фильтра по status."""
    block = (
        await db.execute(
            select(TrainingBlock).where(
                TrainingBlock.id == block_id,
                TrainingBlock.app_user_id == current_user.id,
            )
        )
    ).scalars().first()
    if block is None:
        raise HTTPException(status_code=404, detail="Блок не найден")

    entry = block.entry_state or {}
    exit_state = block.exit_state or {}

    def _as_exercise_id(key: str) -> int | None:
        # Ревью, Находка 3: раньше служебные ключи снимка (напр. "_chronic_level")
        # отсекались по префиксу подчёркивания — фильтр держался на негласном
        # соглашении, что ВСЕ остальные ключи снимка это строковые id упражнений.
        # Пытаемся распарсить ключ напрямую и молча пропускаем то, что не
        # получилось: снимок — данные, накопленные за месяцы существования блока,
        # и один неожиданный/будущий служебный ключ не должен ронять 500-й
        # экран итогов — лучше недосчитать одно упражнение, чем весь эндпоинт.
        try:
            return int(key)
        except (TypeError, ValueError):
            return None

    ids = sorted(
        {
            exercise_id
            for k in list(entry.keys()) + list(exit_state.keys())
            if (exercise_id := _as_exercise_id(k)) is not None
        }
    )

    names: dict[int, str] = {}
    if ids:
        rows = (
            await db.execute(select(Exercise.id, Exercise.name).where(Exercise.id.in_(ids)))
        ).all()
        names = {row.id: row.name for row in rows}

    exercises: list[BlockExerciseSummary] = []
    for exercise_id in ids:
        before = (entry.get(str(exercise_id)) or {}).get("working_e1rm")
        after = (exit_state.get(str(exercise_id)) or {}).get("working_e1rm")
        delta = None
        if before and after:
            delta = round((after - before) / before * 100, 1)
        exercises.append(
            BlockExerciseSummary(
                exercise_id=exercise_id,
                name=names.get(exercise_id),
                entry_e1rm=before,
                exit_e1rm=after,
                delta_pct=delta,
                stalled=bool((exit_state.get(str(exercise_id)) or {}).get("stalled")),
            )
        )

    had_early_deload = (
        await db.execute(
            select(PeriodizationProposal.id).where(
                PeriodizationProposal.block_id == block.id,
                PeriodizationProposal.kind == params.KIND_EARLY_DELOAD,
                PeriodizationProposal.status == params.STATUS_ACCEPTED,
            ).limit(1)
        )
    ).scalars().first() is not None

    return BlockSummaryRead(
        block_id=block.id,
        block_index=block.block_index,
        start_date=block.start_date,
        planned_end_date=block.planned_end_date,
        actual_end_date=block.actual_end_date,
        status=block.status,
        close_reason=block.close_reason,
        had_early_deload=had_early_deload,
        exercises=exercises,
    )
