"""Ручки периодизации (P0-08, Задача 11)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.errors import LocalizedHTTPException
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
from api.services.goal import params as goal_params
from api.services.periodization import params
from api.services.periodization.repository import get_active_block
from api.services.periodization.service import (
    apply_decision,
    block_coordinate,
    safe_refresh_proposals,
)
from api.services.scheduling_engine import SchedulingEngine

router = APIRouter(prefix="/periodization", tags=["Periodization"])


# ИСПРАВЛЕНО (ревью Задачи 15, Critical 2 — карточка цели уходила на экран
# без текста причины): у каждого вида предложения свой словарь причин —
# params.REASON_TEXTS знает коды early_deload/postpone_deload/block_boundary/
# structural/volume_review, а коды автопилота цели (lift_missing,
# pace_behind, partial_catchup, no_lever_left, above_ceiling, trend_down)
# живут в goal_params.REASON_TEXTS и с первым словарём не пересекаются
# вовсе. _proposal_out раньше резолвил reason_text ИСКЛЮЧИТЕЛЬНО через
# params.REASON_TEXTS для любого kind — goal_plan получал "" всегда, и
# /periodization/context (тот самый эндпоинт, который читает карточка цели
# на Home) отдавал пустую строку под заголовком «Цель под угрозой срока».
# На мобильном клиенте уже был обходной локальный словарь для volume_review
# (см. features/periodization/components/ProposalCard.tsx) — заводить
# второй такой же обход для goal_plan не стали: чиним у источника, единым
# правилом «словарь причин выбирается по kind предложения», чтобы текст был
# верным для ЛЮБОГО потребителя контекста, а не только для одного экрана.
_REASON_TEXTS_BY_KIND: dict[str, dict[str, str]] = {
    params.KIND_GOAL_PLAN: goal_params.REASON_TEXTS,
}


def _proposal_out(row: PeriodizationProposal) -> ProposalRead:
    reason_texts = _REASON_TEXTS_BY_KIND.get(row.kind, params.REASON_TEXTS)
    return ProposalRead(
        id=row.id,
        block_id=row.block_id,
        kind=row.kind,
        reason_code=row.reason_code,
        reason_text=reason_texts.get(row.reason_code, ""),
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

    # Разрыв (Critical): фильтр по block_id активного блока был неверным.
    # Карточка итогов (block_boundary) и структурные предложения по вставшим
    # упражнениям (structural) материализуются service.refresh_proposals ПО
    # БЛОКУ, КОТОРЫЙ ТОЛЬКО ЧТО ЗАКРЫЛ автопереход, — это осознанное решение
    # Задачи 9 (см. её докстринг), иначе итогов пройденного блока не увидел
    # бы никто. К моменту запроса активный блок — уже СЛЕДУЮЩИЙ, другой. Фильтр
    # `block_id == block.id` эти предложения не находил НИКОГДА: весь путь
    # "увидеть разбор блока и поправить вставшие упражнения" был мёртв, хотя
    # refresh_proposals их честно создавал. Предложения принадлежат
    # ПОЛЬЗОВАТЕЛЮ, а не конкретному блоку — фильтруем по app_user_id и
    # статусу, отдаём все висящие независимо от того, к какому блоку они
    # привязаны; клиент отличает их друг от друга по полю block_id в ответе.
    pending = (
        await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.app_user_id == current_user.id,
                PeriodizationProposal.status == params.STATUS_PENDING,
            ).order_by(PeriodizationProposal.id)
        )
    ).scalars().all()

    # Поправка 2 брифа Задачи 11: workouts_to_deload — настоящий подсчёт по
    # repository.count_workouts_to_deload (та же функция, что зовёт
    # repository.collect_decision_input для решателя), а не заглушка None.
    # P0-08, Задача 13: сборка координаты вынесена в service.block_coordinate,
    # чтобы не дублировать её со сборкой в build_context workout-центра.
    coordinate = await block_coordinate(
        db,
        current_user.id,
        block,
        today,
        include_workouts_to_deload=True,
        language=getattr(current_user, "_request_language", "en"),
    )

    return PeriodizationContextRead(
        block=coordinate,
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
        db, current_user.id, proposal_id, payload.action, payload.client_uuid,
        options=payload.options,
    )
    if result["status"] == "not_found":
        raise LocalizedHTTPException(404, "periodization.proposal_not_found")
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
        raise LocalizedHTTPException(404, "periodization.block_not_found")

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
