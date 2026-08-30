"""Склейка периодизации: пересчёт предложений и применение решений."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    cast as sa_cast,
    delete as sa_delete,
    or_ as sa_or,
    select,
    update as sa_update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.i18n import SupportedLanguage
from api.schemas.periodization import BlockCoordinateRead
from api.services.models import (
    Exercise,
    Mesocycle,
    PeriodizationProposal,
    TrainingBlock,
    UserCalendarDay,
    UserExerciseRepOverride,
    WorkoutSession,
)
from api.services.periodization import params
from api.services.periodization import phases as phase_ops
from api.services.periodization.decide import decide
from api.services.periodization.position import position
from api.services.periodization.repository import (
    block_exercise_ids,
    block_state,
    close_and_advance,
    collect_decision_input,
    count_workouts_to_deload,
    ensure_active_block,
    get_active_block,
    roll_over_if_complete,
)
from api.services.progression import params as progression_params
from api.services.structure.mesocycle_presets import phase_name

logger = logging.getLogger(__name__)


async def block_coordinate(
    session: AsyncSession,
    app_user_id: int,
    block: TrainingBlock,
    today: date,
    *,
    include_workouts_to_deload: bool = False,
    language: SupportedLanguage | None = None,
    system_mesocycle: bool | None = None,
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
    if language is not None and system_mesocycle is None:
        system_mesocycle = False
        if block.mesocycle_id is not None:
            global_mesocycle_id = (
                await session.execute(
                    select(Mesocycle.id).where(
                        Mesocycle.id == block.mesocycle_id,
                        Mesocycle.author_id.is_(None),
                    )
                )
            ).scalar_one_or_none()
            system_mesocycle = global_mesocycle_id is not None
    response_phase_name = (
        phase_name(current.effort_tier, language)
        if language is not None and system_mesocycle
        else current.name
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
        phase_name=response_phase_name,
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


# P0-08, финальное ревью, Находка 2 (Important): виды предложений, которые
# истекают, когда закрывается более поздний блок и по нему материализуется
# новая карточка итогов. block_boundary и structural — оба рождаются
# ИСКЛЮЧИТЕЛЬНО из ветки decide()._boundary_proposals (см. decide.py: она
# срабатывает только при inp.position.is_complete, то есть только для уже
# ЗАКРЫТОГО блока) — оба вида, соответственно, разбирают состояние блока,
# который уже в прошлом. early_deload и postpone_deload сюда сознательно не
# входят: они про решение по АКТИВНОМУ блоку прямо сейчас (разгружаться ли
# сегодня), а не про разбор истории, и не теряют смысл от того, что где-то
# закрылся ещё один блок.
_EXPIRE_ON_NEW_BOUNDARY_KINDS = (params.KIND_BLOCK_BOUNDARY, params.KIND_STRUCTURAL)


async def _expire_older_boundary_proposals(
    session: AsyncSession, app_user_id: int, block: TrainingBlock
) -> None:
    """Истечь ещё pending карточки итогов и структурные предложения БОЛЕЕ
    РАННИХ блоков этого пользователя (P0-08, финальное ревью, Находка 2).

    Вызывается ровно тогда, когда только что материализована НОВАЯ карточка
    итогов по блоку `block` (см. вызов в _materialize ниже) — разбор
    позапрошлого блока уже неактуален, его место занял разбор последнего.
    Без этого предложения копятся бессрочно: единственное место, где раньше
    выставлялся params.STATUS_EXPIRED, — конфликт в apply_decision по уже
    закрытому блоку (см. _BLOCK_MUTATING_ACTIONS ниже), а он срабатывает,
    только если пользователь вообще попытался ответить на устаревшую
    карточку. Если пользователь просто ушёл с экрана итогов, не решив ничего
    (штатный и поощряемый спекой выбор «доработать по плану»), карточка
    висела бы pending вечно и заслоняла бы актуальную.

    Сравниваем по НОМЕРУ блока (block_index), а не по дате: у TrainingBlock
    нет надёжной единой даты для такого сравнения — actual_end_date/
    planned_end_date есть не у всех блоков в одинаковом смысле (закрытие
    close_stale_block, layoff, досрочная разгрузка дают разные даты закрытия
    для блоков, которые тем не менее строго упорядочены индексом). block_index
    же — монотонный и уникальный на пользователя (см. докстринг phases.py про
    инварианты блока), поэтому "более ранний блок" однозначно means
    block_index меньше, чем у только что закрытого.
    """
    earlier_block_ids = (
        await session.execute(
            select(TrainingBlock.id).where(
                TrainingBlock.app_user_id == app_user_id,
                TrainingBlock.block_index < block.block_index,
            )
        )
    ).scalars().all()
    if not earlier_block_ids:
        return
    await session.execute(
        sa_update(PeriodizationProposal)
        .where(
            PeriodizationProposal.app_user_id == app_user_id,
            PeriodizationProposal.block_id.in_(earlier_block_ids),
            PeriodizationProposal.kind.in_(_EXPIRE_ON_NEW_BOUNDARY_KINDS),
            PeriodizationProposal.status == params.STATUS_PENDING,
        )
        .values(status=params.STATUS_EXPIRED)
    )
    await session.flush()


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

    # P0-08, финальное ревью, Находка 2: новая карточка итогов (block_boundary)
    # только что материализовалась по блоку `block` — значит, `block` уже
    # закрыт (см. докстринг _expire_older_boundary_proposals: этот kind
    # рождается только при is_complete). Разбор БОЛЕЕ РАННИХ блоков того же
    # пользователя устарел, его место занял разбор этого — истекаем их.
    # Проверяем по `created`, а не по `proposals`: если карточка итогов для
    # ЭТОГО блока уже была создана раньше (обычный повторный пересчёт,
    # задедуплицировалась в цикле выше), истечение уже случилось при её
    # первом появлении и повторять его не нужно.
    if any(p.kind == params.KIND_BLOCK_BOUNDARY for p in created):
        await _expire_older_boundary_proposals(session, app_user_id, block)

    return created


async def close_stale_block(
    session: AsyncSession, app_user_id: int, today: Optional[date] = None
) -> Optional[TrainingBlock]:
    """Закрыть блок, который кончился давно и не получил ни одной сессии
    (P0-08, Задача 14).

    Молча продлевать блок на месяц простоя нельзя: это сломало бы и итоги
    (в них попал бы пустой месяц), и триггеры (усталость за простой обнулится,
    и движок сочтёт человека свежим). Порог params.LAYOFF_DAYS_AFTER_BLOCK_END
    даёт пользователю время увидеть итоги и решить самому — сразу после конца
    блока мы ничего не закрываем (см. test_block_just_past_its_end_is_not_closed_yet).

    Поправка 2 брифа Задачи 14 — как это соотносится с автопереходом
    (repository.roll_over_if_complete/_catch_up_active_block): та цепочка
    ТОЖЕ умеет закрыть блок как layoff, но только после
    params.MAX_CHAIN_ROLLOVERS шагов переката ТОГО ЖЕ шаблона подряд — то
    есть после (MAX_CHAIN_ROLLOVERS × длина блока) дней простоя, которые
    успевают заодно материализовать несколько пустых промежуточных блоков.
    Этот порог (14 дней) на порядок короче любой реалистичной длины блока
    (обычно несколько недель на фазу), поэтому close_stale_block должна
    успеть сработать раньше, чем цепочка автоперехода вообще наберёт ход —
    но только если её вызвать ДО roll_over_if_complete/ensure_active_block
    в одном заходе (см. refresh_proposals ниже: close_stale_block стоит
    ПЕРВОЙ). Если бы порядок был обратным, roll_over_if_complete успел бы
    перекатить блок вперёд на следующий же день после planned_end_date, и
    к моменту вызова close_stale_block блок уже не был бы overdue — порог
    14 дней по этому пути никогда бы не сработал.

    Пути, которые вызывают ensure_active_block НАПРЯМУЮ, минуя
    refresh_proposals (SchedulingEngine.ensure_horizon/launch_and_unroll_plan,
    вызванные не из контекста периодизации), close_stale_block не проходят —
    для них цепочка автоперехода внутри ensure_active_block остаётся
    единственным и достаточным предохранителем от бесконечного простоя.

    Закрытие переиспользует repository.close_and_advance (та же функция,
    что и во ВСЕХ путях закрытия блока — roll_over_if_complete,
    _close_for_layoff, service._close_block) — второй копии арифметики
    planned_end_date/переноса снимка здесь нет. next_start_date=moment (а
    не moment+1, как в service._close_block, рассчитанном на ДОСРОЧНОЕ
    закрытие ещё живого блока) — ровно тот же контракт, что и у
    repository._close_for_layoff: блок и так простаивал, начинать заново
    нужно сегодня, а не откладывать ещё на день.

    ВНИМАНИЕ: функция коммитит текущую транзакцию сессии (см. session.commit()
    ниже) — как и repository.ensure_active_block/roll_over_if_complete, см.
    предупреждение в их докстрингах. Вызывать её нужно ДО того, как вызывающий
    код начал накапливать собственные незакоммиченные изменения в этой сессии.
    """
    moment = today or date.today()
    block = await get_active_block(session, app_user_id)
    if block is None:
        return None

    overdue = (moment - block.planned_end_date).days
    if overdue < params.LAYOFF_DAYS_AFTER_BLOCK_END:
        return None

    recent = (
        await session.execute(
            select(WorkoutSession.id).where(
                WorkoutSession.app_user_id == app_user_id,
                WorkoutSession.status == "finished",
                WorkoutSession.finished_at >= block.planned_end_date,
            ).limit(1)
        )
    ).scalars().first()
    if recent is not None:
        return None

    closed = block
    await close_and_advance(
        session,
        app_user_id,
        block,
        close_reason=params.CLOSE_LAYOFF,
        actual_end_date=block.planned_end_date,
        next_start_date=moment,
        today=moment,
    )
    await session.commit()
    return closed


async def close_block_for_split_change(
    session: AsyncSession,
    app_user_id: int,
    next_start_date: date,
    *,
    today: Optional[date] = None,
) -> Optional[TrainingBlock]:
    """Смена сплита обнуляет координату блока: день недели и структура
    расписания уехали, старая координата (день внутри фазы, day_in_block)
    больше ничего не значит (P0-08, Задача 14).

    Нет активного блока (периодизация не настроена) — закрывать нечего,
    функция ничего не делает; старый путь запуска сплита работает как раньше
    (см. test_split_change_without_periodization_still_works).

    Поправка 4 брифа: следующий блок обязан стартовать с ДАТЫ НОВОГО СПЛИТА
    (next_start_date — это request.start_date вызывающей стороны,
    api/routers/splits.py) — пользователь может запланировать запуск
    сплита наперёд, и next_start_date в этом случае в будущем. Но решение
    "закрывать ли" и снимок состояния на закрытие (chronic_level в
    exit_state, actual_end_date) обязаны опираться на РЕАЛЬНОЕ сегодня, а
    не на будущую дату: compute_readiness внутри close_and_advance ищет
    данные ПО РЕАЛЬНОЙ истории, а истории на дату, которая ещё не
    наступила, разумеется, нет. Та же развилка (today реальный ≠ дата,
    которую видит расписание) уже решена в
    SchedulingEngine.launch_and_unroll_plan — см. её докстринг у вызова
    ensure_active_block(session, app_user_id, date.today()).

    P0-08, Задача 14, ревью, Находка 2 (задокументировано, НЕ баг): симметрично
    случаю из поправки 4 выше, next_start_date может оказаться и в ПРОШЛОМ —
    пользователь указал start_date задним числом. Следующий блок ниже
    создаётся именно с этой прошлой датой, а значит окажется просроченным
    СРАЗУ ЖЕ. ensure_active_block, которую тут же вызовет
    launch_and_unroll_plan (см. ссылку на её докстринг выше), увидит этот
    свежесозданный блок как активный и немедленно погонит его цепным
    автопереходом (_catch_up_active_block) вперёд, пока он не догонит
    сегодняшний день, — по пути материализуя один или несколько пустых
    промежуточных блоков (тренировок в них по определению не было, блок
    прожил только что). Это честное следствие устройства блока с фиксированной
    длиной: блок, начавшийся два месяца назад и длящийся четыре недели, и
    правда уже закончился. Данные при этом не теряются — просто расходуется
    несколько пустых записей block_index, ровно как и при любом другом долгом
    перерыве. См. test_split_change_with_past_start_date_catches_up_to_today.

    Не коммитит сессию: вызывается из api/routers/splits.py МЕЖДУ удалением
    будущих дней календаря и запуском SchedulingEngine.launch_and_unroll_plan
    (поправка 3 брифа) — оба действия обязаны попасть в ОДИН commit
    вызывающей стороны, чтобы при сбое между ними откатились оба, а не
    только одно из двух.
    """
    moment = today or date.today()
    block = await get_active_block(session, app_user_id)
    if block is None:
        return None

    closed = block
    await close_and_advance(
        session,
        app_user_id,
        block,
        close_reason=params.CLOSE_SPLIT_CHANGED,
        actual_end_date=moment,
        next_start_date=next_start_date,
        today=moment,
    )
    return closed


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

    P0-08, Задача 14: close_stale_block вызывается ПЕРВОЙ, ДО
    roll_over_if_complete — она тоже закрывает блок как layoff, но по
    гораздо более раннему порогу (params.LAYOFF_DAYS_AFTER_BLOCK_END,
    считаные дни, а не число блоков подряд). Если бы порядок был обратным,
    roll_over_if_complete успела бы перекатить просроченный блок вперёд
    раньше, чем close_stale_block увидела бы его overdue — см. её докстринг
    за подробный разбор.

    P0-08, Задача 14, ревью, Critical 1: блок, закрытый close_stale_block,
    ОБЯЗАН получить карточку итогов на общих основаниях — ровно как и блок,
    закрытый обычным автопереходом чуть ниже. Прежняя версия этого докстринга
    утверждала обратное ("тренировок в нём не было, подводить нечего"), но
    это неверно: проверка внутри close_stale_block смотрит только на сессии
    ПОСЛЕ planned_end_date блока (простаивал ли пользователь достаточно
    долго), а не на сессии ВНУТРИ блока. Блок вполне мог быть отработан
    целиком и просто не пойман автопереходом, потому что пользователь не
    открывал приложение ни разу за params.LAYOFF_DAYS_AFTER_BLOCK_END дней
    после его конца — до этой правки такой блок закрывался, а карточка
    итогов молча пропадала.
    Единственное законное исключение — блок, в котором ДЕЙСТВИТЕЛЬНО не было
    ни одной завершённой тренировки (block_exercise_ids пуст): подводить
    нечего, это то же самое правило, по которому промежуточные пустые блоки
    цепного автоперехода (см. абзац про Находку 6 выше) карточки не получают.
    """
    moment = today or date.today()

    stale_closed_block = await close_stale_block(session, app_user_id, moment)

    closed_block = await roll_over_if_complete(session, app_user_id, moment)

    block = await ensure_active_block(session, app_user_id, moment)
    if block is None:
        return []

    created: list[PeriodizationProposal] = []

    if stale_closed_block is not None:
        if await block_exercise_ids(session, app_user_id, stale_closed_block):
            created.extend(
                await _materialize(session, app_user_id, stale_closed_block, moment)
            )

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
      откатываем ровно SAVEPOINT, не трогая ничего, что было флашено до
      входа в блок;
    - исключение прилетело ПОСЛЕ того, как refresh_proposals успела сама
      закоммитить (roll_over_if_complete/ensure_active_block умеют коммитить
      сессию целиком, см. их докстринги) — тогда откатывать уже нечего, то,
      что успело закоммититься, так и остаётся закоммиченным, ровно как и
      было бы без этой правки. В обоих случаях успешный путь по-прежнему
      коммитит как раньше.

    Найдено при работе над разрывом висящих предложений (P0-08, фикс): у
    `session.commit()` в SQLAlchemy НЕТ понятия "закоммитить только текущий
    SAVEPOINT" — согласно её собственной документации ("The outermost
    database transaction is committed unconditionally, automatically
    releasing any SAVEPOINTs in effect"), ЛЮБОЙ session.commit() внутри
    close_stale_block/roll_over_if_complete/ensure_active_block всегда
    коммитит КОРНЕВУЮ транзакцию целиком, безусловно освобождая наш SAVEPOINT
    — независимо от того, что мы формально ещё "внутри" `async with
    session.begin_nested()`. Раньше здесь стоял именно `async with
    session.begin_nested(): return await refresh_proposals(...)` — и это
    БИЛОСЬ ровно в сценарии, ради которого функция и была написана: как
    только refresh_proposals успевала хоть раз закоммитить (а она делает это
    почти при каждом реальном автопереходе — закрытом блоке), СЛЕДУЮЩИЙ ЖЕ
    `session.execute()` внутри неё (например повторный get_active_block из
    ensure_active_block/_catch_up_active_block, вызываемого сразу вслед за
    roll_over_if_complete) падал с
    `InvalidRequestError: Can't operate on closed transaction inside context
    manager` — потому что `async with` регистрирует себя как "владельца"
    транзакционного контекста при входе (`__aenter__`) и требует, чтобы ЭТА
    ЖЕ транзакция была ещё жива при каждой следующей команде сессии, а её уже
    нет — commit() её закрыл. Этот except ловил исключение молча (ровно как и
    задумано — "сломанная надстройка не должна ронять основной путь"), поэтому
    баг был незаметен: карточка итогов блока (block_boundary) и структурные
    предложения НИКОГДА не материализовались через настоящий вызов
    /periodization/context при реальном автопереходе — _materialize для
    закрытого блока просто не успевал выполниться, roll_over_if_complete
    падал на первом же обращении к сессии ВНУТРИ ensure_active_block. Все
    существующие тесты на KIND_BLOCK_BOUNDARY звали refresh_proposals()
    НАПРЯМУЮ, минуя safe_refresh_proposals и её SAVEPOINT, поэтому не ловили
    этого.

    Чиним, НЕ используя `async with`: `await session.begin_nested()` (без
    контекст-менеджера) тоже открывает настоящий SAVEPOINT, но не
    регистрирует себя во внутреннем `_trans_context_manager` — эту
    регистрацию делает только `__aenter__` (см. `StartableContext.start(...,
    is_ctxmanager=True)` в sqlalchemy.ext.asyncio). Управляем commit/rollback
    вручную и ТОЛЬКО если SAVEPOINT ещё жив (`nested.is_active`) — если
    refresh_proposals уже успела закоммитить корневую транзакцию сама,
    SAVEPOINT к этому моменту уже освобождён, и трогать его снова нельзя.
    """
    nested = await session.begin_nested()
    try:
        result = await refresh_proposals(session, app_user_id, today)
    except Exception:  # noqa: BLE001
        logger.exception("periodization: пересчёт предложений упал")
        if nested.is_active:
            await nested.rollback()
        return []
    else:
        if nested.is_active:
            await nested.commit()
        return result


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
    """Снести дни календаря БЛОКА с first_future, КРОМЕ несущих факт или решение.

    P0-09 закрывает долг, зафиксированный в §8.1 спеки P0-08. Прежнее
    правило «удаляем всё после first_future» было безопасно ровно до тех
    пор, пока UserCalendarDay не хранил факта. Теперь удаление обходит:

      - дни со status != 'planned' — там уже есть выполнение или пропуск;
      - дни с непустым volume_adjustments — там лежит принятое
        пользователем решение, которое иначе исчезло бы при ближайшей
        вставке разгрузки.

    Уцелевшие дни остаются со своей старой координатой фазы. Это
    сознательный обмен: сохранить факт важнее, чем перерисовать прошедший
    или уже настроенный день.
    """
    await session.execute(
        sa_delete(UserCalendarDay).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.block_id == block.id,
            UserCalendarDay.target_date >= first_future,
            UserCalendarDay.status == "planned",
            sa_or(
                UserCalendarDay.volume_adjustments.is_(None),
                UserCalendarDay.volume_adjustments == sa_cast([], JSONB),
            ),
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
    именно этого и попросил. С P0-09 «переписывается» — не безусловно:
    _wipe_future_calendar сама не трогает сегодняшний день, если он несёт
    факт (status != 'planned') или принятую пользователем правку
    (непустой volume_adjustments) — такой день остаётся на старой
    координате фазы, и include_today в этом случае ничего не меняет.
    Обещание переключателя «сегодня стало первым днём выбранной фазы»
    держится ровно до тех пор, пока сегодняшний день ещё ничем не занят;
    если он уже отработан или под него принято решение, сохранить факт
    важнее, чем перерисовать его под новую фазу.

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
    options: Optional[dict] = None,
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
        # P0-12: отмена по определению приходит к УЖЕ применённому
        # предложению — единственное действие, которому статус accepted не
        # помеха.
        if not (
            proposal.kind == params.KIND_GOAL_PLAN
            and action == params.ACTION_UNDO_GOAL
            and proposal.status == params.STATUS_ACCEPTED
        ):
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

    if proposal.kind == params.KIND_GOAL_PLAN:
        # P0-12: автопилот цели — третий вид решения в той же таблице.
        # Стоит до загрузки TrainingBlock и до проверки
        # _BLOCK_MUTATING_ACTIONS по той же причине, что и volume_review:
        # блок здесь координата, а не объект правки.
        from api.services.goal.service import apply_goal_decision, undo_goal_decision

        if action == params.ACTION_UNDO_GOAL:
            # ИСПРАВЛЕНО (ревью Задачи 10 Task-10, Important — откат не был
            # идемпотентен по client_uuid): client_uuid передаём внутрь, а не
            # проставляем здесь безусловно — undo_goal_decision пишет его
            # ТОЛЬКО вместе с остальными decided-полями на успешном пути (см.
            # её докстринг), а не на ранних return (нечего отменять/конфликт
            # по факту), которые ещё не решение.
            outcome = await undo_goal_decision(session, app_user_id, proposal, client_uuid)
            await session.commit()
            return outcome

        if action == params.ACTION_APPLY_GOAL:
            # ИСПРАВЛЕНО (ревью Задачи 10, Critical 1 — та же несущая
            # конструкция, что и у _BLOCK_MUTATING_ACTIONS ниже по файлу, см.
            # комментарий у Critical 3 там). Поля решения выставляем ДО вызова
            # apply_goal_decision, а не после её return. Среди принятых
            # рычагов может оказаться структурный (LEVER_LIFT_FREQUENCY/
            # LEVER_STRUCTURAL) — тогда apply_goal_decision доходит до
            # _apply_structural -> SchedulingEngine.generate_block_days, а та
            # КОММИТИТ СЕССИЮ САМА (см. докстринг _apply_structural). Раньше
            # decided-поля писались только ПОСЛЕ return — обрыв процесса в
            # окне между внутренним commit'ом регенерации и финальным
            # commit'ом здесь оставлял календарь уже перегенерированным и
            # snapshot["structural_applied"] уже True, а proposal.status
            # всё ещё pending. Повтор запроса тогда видел бы structural_
            # applied=True, apply_goal_decision вернула бы applied=[]
            # (структурному рычагу уже нечего применять), и предложение
            # навсегда помечалось бы declined — хотя рычаг фактически
            # применён, календарь реально изменён, а откат (undo_goal_
            # decision) гейтится именно на status == accepted и стал бы для
            # этого предложения недостижим НАВСЕГДА.
            # Ставим оптимистично accepted СЕЙЧАС: обрыв процесса в этом окне
            # оставит правдивую accepted-строку, чьи изменения откат сможет
            # отменить. Если вызов благополучно вернётся и окажется, что
            # НИЧЕГО не применилось (applied пуст), понижаем до declined уже
            # после — см. ниже.
            proposal.status = params.STATUS_ACCEPTED
            proposal.decided_action = action
            proposal.client_uuid = client_uuid
            proposal.decided_at = datetime.now(timezone.utc)

            outcome = await apply_goal_decision(
                session, app_user_id, proposal, action, options or {}
            )
            if not outcome["applied"]:
                proposal.status = params.STATUS_DECLINED
        else:
            outcome = {"status": "declined", "proposal_id": proposal.id, "applied": []}
            proposal.status = params.STATUS_DECLINED
            proposal.decided_action = action
            proposal.client_uuid = client_uuid
            proposal.decided_at = datetime.now(timezone.utc)

        await session.commit()
        return outcome

    if proposal.kind == params.KIND_VOLUME_REVIEW:
        # P0-09, Задача 11: обзор объёма — свой вид решения в той же таблице
        # (см. докстринг KIND_VOLUME_REVIEW). Не блок-мутирующее действие и
        # не структурная замена — TrainingBlock здесь не нужен, поэтому ветка
        # стоит ДО его загрузки и ДО проверки _BLOCK_MUTATING_ACTIONS, которая
        # смысла для этого вида не имеет.
        from api.services.volume.service import apply_volume_decision

        # Ревью Задачи 11, Minor: `action` раньше не проверялся вовсе — ЛЮБАЯ
        # строка доходила до apply_volume_decision, а решение о том, что
        # именно применять, целиком отдавалось `options["accepted"]`. Это
        # значит, что `action="decline"` с непустым (по ошибке клиента или
        # чужого вызова) `accepted` всё равно применил бы правки — состояние
        # решения полностью управлялось бы телом запроса, а не заявленным
        # действием. Валидируем: применяем ТОЛЬКО при action ==
        # ACTION_APPLY_VOLUME (ради которого константа и заведена в
        # params.py); любое другое действие — безусловный отказ, `accepted`
        # игнорируется целиком, а не просто "не находится".
        if action == params.ACTION_APPLY_VOLUME:
            outcome = await apply_volume_decision(
                session, app_user_id, proposal, action, options or {}
            )
        else:
            outcome = {
                "status": "declined",
                "proposal_id": proposal.id,
                "applied": [],
            }
        proposal.status = (
            params.STATUS_ACCEPTED
            if outcome["applied"]
            else params.STATUS_DECLINED
        )
        # Каждая другая ветка apply_decision проставляет decided_action —
        # эта не была исключением по замыслу, просто забылась (ревью Задачи
        # 11, Minor): конфликтный ответ (см. проверку статуса выше по
        # функции) для обзора объёма отдавал бы decided_action: null.
        proposal.decided_action = action
        proposal.client_uuid = client_uuid
        proposal.decided_at = datetime.now(timezone.utc)
        # Брифовый набросок этой ветки заканчивался на flush() — по аналогии
        # с остальной apply_decision (proposal.status и мутации applyVolume
        # копятся в ОДНОЙ сессии, которую эндпоинт больше нигде не коммитит:
        # get_db не автокоммитит на выходе, см. app/database.SessionLocal).
        # Без явного commit() здесь решение и правки бюджета/предписания
        # молча терялись бы при реальном вызове через роутер — тесты этой
        # ветки коммитят сами и бага не поймали бы.
        await session.commit()
        return outcome

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
