"""Склейка периодизации: пересчёт предложений и применение решений."""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


def _dedup_key(kind: str, reason_code: str, payload: dict) -> tuple:
    """Ключ дедупликации pending-предложений ОДНОГО блока (P0-08, Задача 9,
    ревью, Находка 1).

    Три вида предложений различаются по тому, ЧТО именно они дублируют:

    - early_deload и postpone_deload — решения по блоку ЦЕЛИКОМ ("разгружаться
      ли сейчас"), а не по конкретному поводу. Ключ — ТОЛЬКО kind, БЕЗ
      reason_code: решатель выдаёт максимум одно такое предложение за вызов
      (decide() сама это гарантирует), но повод между двумя пересчётами может
      смениться, пока пользователь не ответил на первую карточку (сегодня
      высокая усталость, через неделю она прошла, зато набралось плато). Если
      бы reason_code входил в ключ, смена повода породила бы ВТОРУЮ карточку
      рядом с неотвеченной первой — про то же самое решение. Одновременно
      может существовать не более одной pending-карточки такого рода.
    - structural — решение ПО КОНКРЕТНОМУ УПРАЖНЕНИЮ, из нескольких вставших
      упражнений decide() выдаёт по одному Proposal на каждое. Ключ обязан
      включать exercise_id — иначе дедупликация приняла бы предложение по
      одному упражнению за дубликат предложения по другому, и материализовалось
      бы только первое.
    - block_boundary — ровно одно предложение на блок, без вариаций ни по
      reason_code, ни по упражнению. Ключа kind достаточно.
    """
    if kind == params.KIND_STRUCTURAL:
        return (kind, payload.get("exercise_id"))
    return (kind,)


async def _materialize(
    session: AsyncSession, app_user_id: int, block: TrainingBlock, today: date
) -> list[PeriodizationProposal]:
    """Пересчитать вход решателя ПО ОДНОМУ блоку и материализовать новые
    предложения, не дублируя уже ожидающие ответа пользователя.

    Дедупликация — см. _dedup_key: для early_deload/postpone_deload/
    block_boundary достаточно kind, для structural нужен ещё exercise_id.
    Пересчёт вызывается на каждом обращении к контексту, и без дедупликации
    карточка размножилась бы на каждое открытие экрана.
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
    known = {_dedup_key(p.kind, p.reason_code, p.payload) for p in existing}

    created: list[PeriodizationProposal] = []
    for proposal in proposals:
        key = _dedup_key(proposal.kind, proposal.reason_code, proposal.payload)
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
        # P0-08, Задача 9, ревью, Находка 2: чтение existing выше и вставка
        # здесь неатомарны — конкурентный вызов (мобильный клиент на старте
        # дёргает контекст дня и контекст периодизации одновременно, см.
        # комментарии про эту же гонку в repository.ensure_active_block) мог
        # пройти своё чтение до нашего коммита и сейчас вставляет ТУ ЖЕ
        # строку. uq_periodization_proposals_pending (app/database.py) ловит
        # это на уровне БД. SAVEPOINT ограничивает откат при коллизии ОДНОЙ
        # этой вставкой — без него IntegrityError испортил бы всю транзакцию,
        # включая proposals, уже успешно вставленные на предыдущих итерациях
        # этого же цикла.
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            # Гонка сработала ровно так, как задумано индексом: конкурентный
            # запрос уже вставил и закоммитил такое же pending-предложение.
            # Это не ошибка, а именно тот исход, ради которого индекс
            # поставлен — считаем предложение уже созданным и идём дальше.
            known.add(key)
            continue
        known.add(key)
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

    P0-08, Задача 9, ревью, Находка 6: при многозвенном автопереходе
    (пользователь отсутствовал дольше одной длины блока) roll_over_if_complete
    перекатывает ровно ОДИН шаг за вызов, а цепочку из нескольких просроченных
    блоков подряд доперекатывает _catch_up_active_block ВНУТРИ
    ensure_active_block (см. её докстринг). Из всей этой цепочки закрытых
    блоков карточку итогов (block_boundary) получает только ТОТ ОДИН, что
    закрыла явная roll_over_if_complete выше, — промежуточные блоки,
    закрытые внутри ensure_active_block по пути, карточкой не покрываются.
    Это осознанное поведение, а не упущение: промежуточные блоки пусты
    (тренировок в них не было — человек был в отпуске), а карточка "вот ваши
    итоги" по блоку без единой тренировки была бы чистым шумом.
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

    P0-08, Задача 9, ревью, Находка 3: работа обёрнута в session.begin_nested()
    (SAVEPOINT), а не в голый try/except с session.rollback(). Функция пока
    нигде не подключена, но её подключат в Задачи 11 и 13 — ВНУТРЬ эндпоинтов,
    собирающих контекст, где к моменту вызова в ТОЙ ЖЕ сессии уже могут
    лежать чужие незакоммиченные изменения (например обновлённый калькулятор
    дня). Голый session.rollback() откатывает ВСЮ транзакцию сессии с самого
    начала — упавший пересчёт периодизации утащил бы за собой и эту чужую
    работу, хотя задача функции ровно противоположная: не уронить основной
    путь целиком.

    begin_nested() перед началом работы сам флашит всё, что уже накоплено в
    сессии, — это уходит в ОБЪЕМЛЮЩУЮ транзакцию, а не в SAVEPOINT, и потому
    переживает откат SAVEPOINT. Дальше возможны два случая:
    - исключение прилетело БЕЗ промежуточного session.commit() внутри —
      выход из `async with` откатывает ровно SAVEPOINT, не трогая ничего, что
      было флашено до входа в блок;
    - исключение прилетело ПОСЛЕ того, как refresh_proposals успела сама
      закоммитить (roll_over_if_complete/ensure_active_block умеют коммитить
      сессию целиком, см. их докстринги) — тогда откатывать уже нечего, то,
      что успело закоммититься, так и остаётся закоммиченным, ровно как и
      было бы без этой правки. В обоих случаях успешный путь по-прежнему
      коммитит как раньше — SAVEPOINT просто снимается при выходе без
      исключения.
    """
    try:
        async with session.begin_nested():
            return await refresh_proposals(session, app_user_id, today)
    except Exception:  # noqa: BLE001
        logger.exception("periodization: пересчёт предложений упал")
        return []
