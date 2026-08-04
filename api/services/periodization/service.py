"""Склейка периодизации: пересчёт предложений и применение решений."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas.periodization import BlockCoordinateRead
from api.services.models import (
    Exercise,
    PeriodizationProposal,
    TrainingBlock,
    UserCalendarDay,
    UserExerciseRepOverride,
)
from api.services.periodization import params
from api.services.periodization import phases as phase_ops
from api.services.periodization.decide import decide
from api.services.periodization.position import position
from api.services.periodization.repository import (
    block_state,
    close_and_advance,
    collect_decision_input,
    count_workouts_to_deload,
    ensure_active_block,
    roll_over_if_complete,
)
from api.services.progression import params as progression_params

logger = logging.getLogger(__name__)


async def block_coordinate(
    session: AsyncSession,
    app_user_id: int,
    block: TrainingBlock,
    today: date,
    *,
    include_workouts_to_deload: bool = False,
) -> BlockCoordinateRead:
    """Координата блока на дату — единая сборка BlockCoordinateRead (P0-08,
    Задача 13).

    Раньше эта сборка была списана дважды: в api/routers/periodization.py
    (get_periodization_context) и в брифе Задачи 13 предлагалась третья копия
    прямо в build_context workout-центра. Вынесена сюда, в склейку
    периодизации, а не в repository.py — repository.py сознательно не знает
    про HTTP-схемы (её докстринг: "Ядро остаётся чистым"), а BlockCoordinateRead
    это уже DTO контракта, а не доменный объект.

    workouts_to_deload считается отдельным запросом (repository.count_workouts_to_deload)
    и включается по флагу: HTTP-контракту периодизации (Задача 11) он нужен
    как настоящее число, контексту workout-центра — нет (см. поле =None в
    брифе Задачи 13), лишний запрос на каждое открытие Home того не стоит.
    """
    state = block_state(block)
    pos = position(state, today)
    current = next(
        (p for p in state.phases if p.phase_number == pos.phase_number), state.phases[-1]
    )
    workouts_to_deload = None
    if include_workouts_to_deload:
        workouts_to_deload = await count_workouts_to_deload(
            session, app_user_id, today, pos.days_to_deload
        )
    return BlockCoordinateRead(
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
    )


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


# --- Применение решений (P0-08, Задача 10) -----------------------------------


# P0-08, Задача 10, ревью, Critical 1+2: действия, которые правят ЖИВОЙ снимок
# блока (phases/planned_end_date) и/или заводят следующий блок. Предложение
# может провисеть pending дольше, чем блок остаётся активным: автопереход
# (repository.roll_over_if_complete) закрывает истёкший блок и открывает
# следующий НЕЗАВИСИМО от того, ответил ли пользователь на карточку — apply_decision
# смотрел только на proposal.status и никогда не проверял состояние блока.
#
# Если применить insert_deload/postpone к уже ЗАКРЫТОМУ блоку, _regenerate_future
# распишет дни календаря на даты, уже покрытые днями НОВОГО активного блока —
# на выходе два UserCalendarDay на одну дату, и GET /calendar/day падает с
# MultipleResultsFound (Critical 1). Если применить close_block к уже
# закрытому блоку, close_and_advance попытается создать следующий блок с
# индексом, который уже занял блок автоперехода, и получит необработанный
# IntegrityError от uq_training_blocks_user_index — 500 вместо контрактного
# ответа о конфликте (Critical 2).
#
# start_next_block, структурные действия (OPTION_SHIFT_REPS/REPLACE/KEEP) и
# decline сюда СОЗНАТЕЛЬНО не входят и проверяться не должны — у них другая
# природа предложения, а не побочный эффект той же гонки:
# - карточка итогов блока (kind=block_boundary, действие start_next_block)
#   ПО ПОСТРОЕНИЮ материализуется по уже ЗАКРЫТОМУ блоку — это нормальный
#   дизайн Задачи 9 (см. refresh_proposals: _materialize(..., closed_block, ...)),
#   а не проблема. Добавь сюда start_next_block — и КАЖДОЕ применение
#   карточки итогов начало бы ошибочно считаться "устаревшим", хотя блок
#   закрыт ровно тем автопереходом, который эту карточку и породил.
# - структурные действия (Задача 12) тоже отвечают на карточку по закрытому
#   блоку — структурная правка меняет упражнение/схему, а не снимок фаз
#   блока, поэтому дублирования дней календаря или коллизии индекса блока
#   здесь в принципе не возникает.
# - decline не меняет блок ни при каком его статусе — проверять нечего.
_BLOCK_MUTATING_ACTIONS = frozenset({"insert_deload", "close_block", "postpone"})


async def _wipe_future_calendar(
    session: AsyncSession, app_user_id: int, block: TrainingBlock, first_future: date
) -> None:
    """Снести дни календаря БЛОКА строго с first_future и дальше.

    Прошлое неприкосновенно: сегодня это безопасно только потому, что
    UserCalendarDay не хранит факт (status всегда planned, ссылки на
    завершённую сессию нет). Когда P0-09 добавит учёт факта, это правило
    придётся ужесточить.
    """
    await session.execute(
        sa_delete(UserCalendarDay).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.block_id == block.id,
            UserCalendarDay.target_date >= first_future,
        )
    )
    await session.flush()


async def _generate_future_calendar(
    session: AsyncSession, app_user_id: int, block: TrainingBlock, first_future: date
) -> None:
    from api.services.scheduling_engine import SchedulingEngine

    await SchedulingEngine.generate_block_days(
        session, app_user_id, block,
        from_date=first_future, until_date=block.planned_end_date,
    )


async def _regenerate_future(
    session: AsyncSession, app_user_id: int, block: TrainingBlock, today: date,
    *, include_today: bool = False,
) -> None:
    """Снести и пересобрать дни ЭТОГО ЖЕ блока строго ПОСЛЕ сегодняшнего
    (либо, если include_today=True, начиная с сегодняшнего включительно).

    Годится для insert_deload/postpone — блок продолжается тем же самым,
    меняется только его будущее. Для закрытия блока (action="close_block")
    это НЕ подходит: там дни старого блока нужно снести, а сгенерировать —
    уже для НОВОГО блока (см. _close_block ниже и её докстринг про
    столкновение дат).

    P0-08, Задача 13, ревью, Critical 2: include_today — специальный случай
    ИСКЛЮЧИТЕЛЬНО для переключателя фазы (api/routers/workout_center.py,
    set_active_mesocycle_phase). Переключатель прямо обещает пользователю
    «сегодня становится первым днём выбранной фазы»: обычная перегенерация
    (только ПОСЛЕ сегодня) трогает дни строго после today, и если день на
    сегодня уже материализован (обычный случай для блока, идущего не первый
    день), без include_today он навсегда остался бы со старой фазой —
    /calendar/day/{сегодня} и контекст workout-центра показывали бы разные
    фазы в один и тот же момент, а тренировка, начатая с календаря, унесла бы
    старую фазу дальше.

    Это НЕ нарушает инвариант «прошлое неприкосновенно» (см.
    _wipe_future_calendar): неприкосновенны дни ДО сегодняшнего, а
    сегодняшний день переписывается по прямой команде пользователя, который
    именно этого и попросил. Перезапись ничего не теряет: UserCalendarDay
    сейчас не хранит факт (status всегда "planned", ссылки на завершённую
    сессию нет) — когда P0-09 научит календарь помнить факт, это место
    придётся пересмотреть, как и сам _wipe_future_calendar.

    insert_deload/postpone (ниже в _perform) НЕ передают include_today —
    для них поведение остаётся прежним: перегенерация только будущего.
    """
    first_future = today if include_today else today + timedelta(days=1)
    await _wipe_future_calendar(session, app_user_id, block, first_future)
    await _generate_future_calendar(session, app_user_id, block, first_future)


def _recompute_planned_end(block: TrainingBlock) -> None:
    """Пересчитать planned_end_date СУЩЕСТВУЮЩЕГО блока после правки его
    снимка фаз (insert_deload/postpone).

    Не то же самое, что арифметика create_block (поправка 2 брифа Задачи
    10, про которую нельзя заводить вторую копию): create_block считает
    planned_end_date у НОВОГО блока по кортежу PhaseSnapshot ДО записи в
    колонку; здесь блок уже существует, его phases уже переписаны в JSON
    (phase_ops.to_json), и пересчитывается конец уже сидящего в сессии
    объекта. Разные входы, разный момент — переиспользовать create_block
    для этого нельзя, а формула в две строки не стоит собственной функции
    в repository.py.
    """
    total = sum(int(p["length_days"]) for p in block.phases)
    block.planned_end_date = block.start_date + timedelta(days=total - 1)


async def _close_block(
    session: AsyncSession, app_user_id: int, block: TrainingBlock, reason: str, today: date
) -> TrainingBlock:
    """Закрыть блок ДОСРОЧНОЙ разгрузкой и открыть следующий с тем же
    состоянием.

    Поправка 2 брифа Задачи 10: закрытие+перенос состояния уже сделаны в
    repository.close_and_advance (общая часть с roll_over_if_complete и
    _close_for_layoff) — здесь НЕ пишем вторую копию этой логики, только
    вызываем её и довешиваем то, что специфично именно для пользовательского
    решения: досрочное закрытие происходит РАНЬШЕ planned_end_date блока, а
    значит календарь мог быть уже сгенерирован наперёд вплоть до старой
    границы. Если не снести эти дни СТАРОГО блока, они останутся в базе и
    столкнутся по датам с днями, которые сейчас сгенерируем для НОВОГО —
    GET /calendar/day делает select(...).scalar_one_or_none() по
    (app_user_id, target_date) и падает с MultipleResultsFound на дубликате.
    """
    first_future = today + timedelta(days=1)
    await _wipe_future_calendar(session, app_user_id, block, first_future)

    nxt = await close_and_advance(
        session,
        app_user_id,
        block,
        close_reason=reason,
        actual_end_date=today,
        next_start_date=first_future,
        today=today,
    )
    await session.flush()
    await _generate_future_calendar(session, app_user_id, nxt, first_future)
    return nxt


async def apply_decision(
    session: AsyncSession,
    app_user_id: int,
    proposal_id: int,
    action: str,
    client_uuid: Optional[str] = None,
    today: Optional[date] = None,
) -> dict:
    """Применить решение пользователя по предложению периодизации.

    Контракт совпадает с принятым в P0-03 для синхронизации: повтор с тем же
    client_uuid возвращает результат первого решения (status=already_applied),
    чужое решение по уже решённому предложению отвечает конфликтом
    (status=conflict), а не тихо перезаписывает его.

    P0-08, Задача 10, ревью, Critical 1+2: тем же конфликтом (status=conflict,
    reason=block_closed) отвечаем и на устаревшее ЕЩЁ pending предложение,
    если действие меняет блок (_BLOCK_MUTATING_ACTIONS), а сам блок уже успел
    закрыться автопереходом, пока пользователь не отвечал на карточку. Такое
    предложение при этом переводим в params.STATUS_EXPIRED — см. докстринг
    _BLOCK_MUTATING_ACTIONS выше за подробности.
    """
    moment = today or date.today()

    proposal = (
        await session.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.id == proposal_id,
                PeriodizationProposal.app_user_id == app_user_id,
            )
        )
    ).scalars().first()
    if proposal is None:
        return {"status": "not_found"}

    if proposal.status != params.STATUS_PENDING:
        if client_uuid and proposal.client_uuid == client_uuid:
            return {
                "status": "already_applied",
                "proposal_id": proposal.id,
                "block_id": proposal.block_id,
            }
        return {
            "status": "conflict",
            "proposal_id": proposal.id,
            "current_status": proposal.status,
            "decided_action": proposal.decided_action,
        }

    block = (
        await session.execute(
            select(TrainingBlock).where(TrainingBlock.id == proposal.block_id)
        )
    ).scalars().first()
    if block is None:
        return {"status": "not_found"}

    # P0-08, Задача 10, ревью, Critical 1+2 — см. докстринг _BLOCK_MUTATING_ACTIONS
    # выше за полное объяснение. proposal.status == pending сам по себе ничего
    # не говорит о свежести предложения: блок, на который оно ссылается, мог
    # закрыться автопереходом уже ПОСЛЕ материализации карточки. Действие,
    # меняющее блок, применённое к закрытому блоку, — это и есть Critical 1/2;
    # проверяем состояние блока ЗДЕСЬ, до вызова _perform, а не полагаемся на
    # то, что _perform как-нибудь сама разберётся (она не разбирается).
    if action in _BLOCK_MUTATING_ACTIONS and block.status != "active":
        # Карточка устарела — показывать её больше незачем, но и оставлять
        # pending нельзя: следующий же повтор запроса попал бы сюда же.
        proposal.status = params.STATUS_EXPIRED
        await session.commit()
        return {
            "status": "conflict",
            "proposal_id": proposal.id,
            "block_id": block.id,
            "reason": "block_closed",
        }

    # P0-08, Задача 10, ревью, Critical 3: поля решения (status/decided_action/
    # client_uuid/decided_at) проставляются ЗДЕСЬ, ДО вызова _perform — это
    # порядок несущей конструкции, а не стиля, и переставлять его обратно
    # нельзя. _perform для insert_deload/close_block/postpone в конце концов
    # доходит до SchedulingEngine.generate_block_days, а та КОММИТИТ СЕССИЮ
    # САМА (см. её докстринг и последнюю строку тела) — до того, как
    # выполнение вернётся сюда и дойдёт до session.commit() ниже. Если бы
    # решение проставлялось ПОСЛЕ _perform (как было раньше), между этими
    # двумя коммитами появлялось окно: правка фаз блока и перегенерированный
    # календарь уже зафиксированы в БД, а proposal.status всё ещё "pending".
    # Прервись процесс в этом окне (обрыв соединения, таймаут, рестарт пода) —
    # повтор запроса (например ретрай мобильного клиента) прошёл бы проверку
    # "status == pending" заново и выполнил бы то же действие ВТОРОЙ раз. Для
    # postpone это особенно разрушительно: phases.postpone_deload не защищена
    # от повторного применения и при втором проходе продлит ту же самую фазу
    # перед разгрузкой ещё раз — разгрузка уедет вдвое дальше плана. Проставляя
    # решение ДО _perform, мы добиваемся, что оба факта — "предложение решено"
    # и "блок изменён" — либо оба попадают в ОДИН И ТОТ ЖЕ commit() внутри
    # _perform (когда он есть), либо ни один не попадает, если процесс
    # прервался раньше. Расщепления на два отдельных коммита больше нет.
    if action == "decline":
        proposal.status = params.STATUS_DECLINED
    else:
        proposal.status = params.STATUS_ACCEPTED

    proposal.decided_action = action
    proposal.client_uuid = client_uuid
    proposal.decided_at = datetime.now(timezone.utc)

    if action != "decline":
        await _perform(session, app_user_id, block, proposal, action, moment)

    await session.commit()
    return {"status": "applied", "proposal_id": proposal.id, "block_id": block.id}


async def _perform(
    session: AsyncSession,
    app_user_id: int,
    block: TrainingBlock,
    proposal: PeriodizationProposal,
    action: str,
    today: date,
) -> None:
    """Что физически делает каждое действие."""
    if action == "insert_deload":
        after = proposal.payload.get("after_phase_number") or position(
            block_state(block), today
        ).phase_number
        updated = phase_ops.insert_deload(
            phase_ops.from_json(block.phases),
            after_phase_number=after,
            length_days=block.microcycle_length,
        )
        block.phases = phase_ops.to_json(updated)
        _recompute_planned_end(block)
        await session.flush()
        await _regenerate_future(session, app_user_id, block, today)

    elif action == "close_block":
        await _close_block(session, app_user_id, block, params.CLOSE_EARLY_DELOAD, today)

    elif action == "start_next_block":
        # P0-08, Задача 10, поправка 1 брифа: переход между блоками теперь
        # АВТОМАТИЧЕСКИЙ (repository.roll_over_if_complete, Задача 7) — блок
        # уже закрыт, а следующий уже открыт к тому моменту, когда
        # пользователь вообще видит карточку итогов (kind=block_boundary).
        # Раньше (до Задачи 7) start_next_block сам закрывал блок и открывал
        # следующий; теперь это действие ничего не меняет в блоках — оно
        # только фиксирует, что пользователь увидел карточку с итогами и
        # ответил на неё. proposal.status/decided_action/decided_at уже
        # выставлены в apply_decision выше — здесь физически делать нечего.
        return

    elif action == "postpone":
        updated = phase_ops.postpone_deload(
            phase_ops.from_json(block.phases), extra_days=block.microcycle_length
        )
        block.phases = phase_ops.to_json(updated)
        _recompute_planned_end(block)
        await session.flush()
        await _regenerate_future(session, app_user_id, block, today)

    elif action == params.OPTION_SHIFT_REPS:
        await _shift_reps(session, app_user_id, proposal)

    elif action in (params.OPTION_REPLACE, params.OPTION_KEEP):
        # replace уводит пользователя в существующий флоу умной замены на
        # клиенте — здесь фиксируется только само решение. keep не делает
        # ничего по определению.
        return


async def _shift_reps(
    session: AsyncSession, app_user_id: int, proposal: PeriodizationProposal
) -> None:
    """Действие «сдвиг диапазона повторов» (Задача 12): заводит или ужимает
    персональный override для упражнения, вставшего даже после разгрузки.

    Идемпотентность ПОВТОРА ОДНОГО И ТОГО ЖЕ решения (двойной тап, повтор из
    офлайн-очереди) обеспечивает не эта функция, а вызывающий её
    apply_decision: решение (proposal.status/client_uuid) коммитится в ОДНОЙ
    транзакции с этой записью (см. комментарий у Critical 3 выше), поэтому
    либо оба факта попадают в БД вместе, либо ни один. Повторный вызов с тем
    же proposal_id находит status != pending и возвращает already_applied/
    conflict, не доходя до _perform повторно.

    ДВА РАЗНЫХ pending-предложения по одному и тому же упражнению (упражнение
    встало во ВТОРОЙ раз, уже после первого сдвига) — легитимный случай, и
    диапазон сдвигается ЕЩЁ РАЗ: это не дубликат, а второе самостоятельное
    решение пользователя. Не уехать в отрицательные/бессмысленные повторы при
    этом не даёт REP_SHIFT_MIN — второй (и любой следующий) сдвиг сходится к
    полу и там останавливается, а не убывает бесконечно.

    Конкурентное ПЕРВОЕ создание override для одного и того же упражнения с
    ДВУХ разных pending-предложений (гонка, а не последовательность) ловит
    уникальный индекс uq_user_exercise_rep_overrides_user_exercise
    (app/database.py) через SAVEPOINT — так же, как _materialize ловит гонку
    вставки предложений: проигравший транзакцию считает, что сдвиг уже
    применён конкурентом, и своего сдвига не делает (иначе на одну пару
    пользователь+упражнение легло бы две строки, и какую из них видит
    _load_rep_overrides — вопрос порядка чтения).
    """
    exercise_id = proposal.payload.get("exercise_id")
    if exercise_id is None:
        return

    current = (
        await session.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == app_user_id,
                UserExerciseRepOverride.exercise_id == exercise_id,
            )
        )
    ).scalars().first()

    if current is not None:
        base_min, base_max = current.rep_min, current.rep_max
    else:
        # Находка 2/3 ревью Задачи 12: базой первого сдвига обязан быть
        # РЕАЛЬНЫЙ диапазон упражнения, а не литералы 8/12 (Находка 3 —
        # магическое число в логике, запрещённое правилами проекта). У
        # тяжёлой базы (fatigue_tier=1) запасной диапазон — 6-8: сдвиг от
        # литералов 8/12 дал бы 5-9, и верхняя граница ВЫРОСЛА БЫ — прямо
        # противоположно смыслу действия "стало тяжело, сузим диапазон".
        # TIER_REP_FALLBACK[2] — тот же запасной диапазон второго тира, что
        # resolvers.py подставляет по умолчанию, когда активного микроцикла
        # нет; здесь он служит той же цели на случай, если exercise_id не
        # нашёлся в справочнике упражнений (не должно случаться, но не
        # повод уронить применение решения).
        fatigue_tier = (
            await session.execute(
                select(Exercise.fatigue_tier).where(Exercise.id == exercise_id)
            )
        ).scalar_one_or_none()
        base_min, base_max = progression_params.TIER_REP_FALLBACK.get(
            fatigue_tier, progression_params.TIER_REP_FALLBACK[2]
        )
    step = progression_params.REP_SHIFT_STEP
    new_min = max(progression_params.REP_SHIFT_MIN, base_min - step)
    new_max = max(new_min + 1, base_max - step)

    if current is not None:
        current.rep_min = new_min
        current.rep_max = new_max
        return

    try:
        async with session.begin_nested():
            session.add(
                UserExerciseRepOverride(
                    app_user_id=app_user_id, exercise_id=exercise_id,
                    rep_min=new_min, rep_max=new_max,
                )
            )
            await session.flush()
    except IntegrityError:
        # Конкурентное предложение по тому же упражнению уже создало override
        # первым — см. докстринг выше. Свой сдвиг не делаем, чтобы не
        # получить вторую строку на ту же пару пользователь+упражнение.
        pass
