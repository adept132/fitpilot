"""Склейка периодизации: пересчёт предложений и применение решений."""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import PeriodizationProposal, TrainingBlock
from api.services.periodization import params
from api.services.periodization.decide import decide
from api.services.periodization.repository import (
    collect_decision_input,
    ensure_active_block,
    roll_over_if_complete,
)

logger = logging.getLogger(__name__)


async def _materialize(
    session: AsyncSession, app_user_id: int, block: TrainingBlock, today: date
) -> list[PeriodizationProposal]:
    """Пересчитать вход решателя ПО ОДНОМУ блоку и материализовать новые
    предложения, не дублируя уже ожидающие ответа пользователя.

    Дедупликация по тройке (kind, reason_code, exercise_id) среди pending
    предложений ЭТОГО блока: пересчёт вызывается на каждом обращении к
    контексту, и без неё карточка размножилась бы на каждое открытие экрана.
    """
    decision_input = await collect_decision_input(session, app_user_id, block, today)
    proposals = decide(decision_input)
    if not proposals:
        return []

    existing = (
        await session.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.block_id == block.id,
                PeriodizationProposal.status == params.STATUS_PENDING,
            )
        )
    ).scalars().all()
    known = {(p.kind, p.reason_code, p.payload.get("exercise_id")) for p in existing}

    created: list[PeriodizationProposal] = []
    for proposal in proposals:
        key = (proposal.kind, proposal.reason_code, proposal.payload.get("exercise_id"))
        if key in known:
            continue
        row = PeriodizationProposal(
            app_user_id=app_user_id,
            block_id=block.id,
            kind=proposal.kind,
            reason_code=proposal.reason_code,
            # P0-08, Задача 9, поправка 3: decide() кладёт в payload["options"]
            # ОДИН И ТОТ ЖЕ объект списка params.STRUCTURAL_OPTIONS во все
            # структурные предложения сразу — payload кладём в колонку как
            # есть, ничего в нём не меняя ни здесь, ни где-либо ниже по стеку.
            payload=proposal.payload,
            status=params.STATUS_PENDING,
        )
        session.add(row)
        created.append(row)

    return created


async def refresh_proposals(
    session: AsyncSession, app_user_id: int, today: Optional[date] = None
) -> list[PeriodizationProposal]:
    """Пересчитать предложения и материализовать новые.

    P0-08, Задача 9, поправка 1: ensure_active_block сама вызывает
    roll_over_if_complete внутри себя и "съедает" переход — у активного
    блока position(...).is_complete практически никогда не бывает True
    (ensure_active_block уже перекатила его дальше), поэтому наивный вызов
    одной только ensure_active_block никогда не породил бы карточку итогов
    блока (block_boundary). Поэтому закрытие проверяем ЯВНО и ПЕРЕД
    ensure_active_block, запоминая закрытый блок:

    1. roll_over_if_complete закрывает истёкший активный блок (если такой
       есть) и возвращает ЕГО — не следующий.
    2. Если блок был закрыт — по НЕМУ (координата с is_complete=True)
       собираем вход решателя и материализуем предложения границы
       (block_boundary плюс структурные по стабильно вставшим упражнениям).
    3. Независимо от этого — обычным порядком по АКТИВНОМУ блоку (который
       ensure_active_block вернёт, доперекатив цепочку при необходимости)
       собираем вход и материализуем его предложения (досрочная разгрузка,
       перенос плановой).

    И roll_over_if_complete, и ensure_active_block коммитят сессию сами —
    поэтому все PeriodizationProposal добавляются в сессию СТРОГО ПОСЛЕ
    обоих вызовов, а не раньше: иначе их подхватило бы чужое внутреннее
    commit() до того, как мы закончили решать, что вообще материализовать.
    """
    moment = today or date.today()

    closed_block = await roll_over_if_complete(session, app_user_id, moment)

    block = await ensure_active_block(session, app_user_id, moment)
    if block is None:
        return []

    created: list[PeriodizationProposal] = []

    if closed_block is not None:
        created.extend(await _materialize(session, app_user_id, closed_block, moment))

    created.extend(await _materialize(session, app_user_id, block, moment))

    if created:
        await session.commit()
    return created


async def safe_refresh_proposals(
    session: AsyncSession, app_user_id: int, today: Optional[date] = None
) -> list[PeriodizationProposal]:
    """Пересчёт, который не роняет вызывающий эндпоинт.

    Тот же принцип, что применён к неразбираемому предписанию в load_history:
    сломанная надстройка не должна ронять основной путь. Тренировка важнее
    карточки с предложением.
    """
    try:
        return await refresh_proposals(session, app_user_id, today)
    except Exception:  # noqa: BLE001
        logger.exception("periodization: пересчёт предложений упал")
        await session.rollback()
        return []
