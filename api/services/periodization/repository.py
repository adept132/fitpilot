"""Единственное место периодизации, знающее про БД.

Ядро (position/phases/decide) остаётся чистым: здесь мы только достаём входы
и материализуем блок.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    AppUserMesocycle,
    AppUserMicrocycle,
    Mesocycle,
    TrainingBlock,
    UserSplit,
    WorkoutSession,
    WorkoutSessionExercise,
)
from api.services.periodization import params, phases as phase_ops
from api.services.periodization.types import BlockState, PhaseSnapshot
from api.services.progression.repository import load_history
from api.services.progression.rounding import step_kg
from api.services.progression.state import rebuild_state


async def _current_chronic_level(
    session: AsyncSession, app_user_id: int, moment: date
) -> Optional[float]:
    """Хронический уровень нагрузки на момент создания/закрытия блока.

    Кладётся в entry_state/exit_state под служебным ключом "_chronic_level"
    (P0-08, Задача 9, поправка 4 брифа): перенос плановой разгрузки
    (decide._postpone) сравнивает ТЕКУЩИЙ chronic_level с базовым значением
    на старте блока, а до этой поправки записывать базовое значение было
    попросту некому — build_state_snapshot пишет только словари по
    упражнениям, ключи которых всегда строковые id (никогда не начинаются с
    подчёркивания). Подчёркивание в имени ключа — единственное, что отличает
    служебное поле от ключа-упражнения при последующем чтении entry_state.
    """
    from datetime import datetime, time, timezone

    from api.services.fatigue.service import compute_readiness

    moment_dt = datetime.combine(moment, time(12, 0), tzinfo=timezone.utc)
    report = await compute_readiness(session, app_user_id, now=moment_dt)
    return report.progression.chronic_level


async def get_active_block(
    session: AsyncSession, app_user_id: int
) -> Optional[TrainingBlock]:
    return (
        await session.execute(
            select(TrainingBlock)
            .where(
                TrainingBlock.app_user_id == app_user_id,
                TrainingBlock.status == "active",
            )
            .order_by(TrainingBlock.block_index.desc())
        )
    ).scalars().first()


def block_state(block: TrainingBlock) -> BlockState:
    return BlockState(
        block_index=block.block_index,
        phases=phase_ops.from_json(block.phases),
        start_date=block.start_date,
        microcycle_length=block.microcycle_length,
    )


async def _snapshot_phases_from_template(
    session: AsyncSession, user_meso: AppUserMesocycle, microcycle_length: int
) -> tuple[PhaseSnapshot, ...]:
    """Снять фазы с шаблона. Длина каждой равна длине микроцикла — то же
    правило, что сегодня зашито в SchedulingEngine (days_per_phase = micro_length)."""
    strategy = (
        await session.execute(
            select(Mesocycle)
            .where(Mesocycle.id == user_meso.mesocycle_id)
            .options(selectinload(Mesocycle.phases))
        )
    ).scalar_one_or_none()
    if strategy is None or not strategy.phases:
        return ()

    return tuple(
        PhaseSnapshot(
            phase_number=p.phase_number,
            name=p.name,
            effort_tier=p.effort_tier,
            length_days=microcycle_length,
        )
        for p in sorted(strategy.phases, key=lambda p: p.phase_number)
    )


async def create_block(
    session: AsyncSession,
    app_user_id: int,
    start_date: date,
    *,
    block_index: int,
    phases: tuple[PhaseSnapshot, ...],
    user_meso: Optional[AppUserMesocycle],
    user_micro: Optional[AppUserMicrocycle],
    split_blueprint_id=None,
    entry_state: Optional[dict] = None,
) -> TrainingBlock:
    if not phases:
        # Пустые фазы дают planned_end_date раньше start_date (total_days=0,
        # минус один день ниже) — публичная функция не должна тихо создавать
        # такой блок, даже если сегодня единственный вызывающий (ensure_active_block)
        # уже отсекает пустой снимок раньше.
        raise ValueError("Блок без фаз создать нельзя")
    total_days = sum(p.length_days for p in phases)
    block = TrainingBlock(
        app_user_id=app_user_id,
        block_index=block_index,
        user_mesocycle_id=user_meso.id if user_meso else None,
        mesocycle_id=user_meso.mesocycle_id if user_meso else None,
        phases=phase_ops.to_json(phases),
        user_microcycle_id=user_micro.id if user_micro else None,
        microcycle_length=user_micro.length_days if user_micro else 7,
        split_blueprint_id=split_blueprint_id,
        start_date=start_date,
        # Минус один день: первый день блока — сам start_date.
        planned_end_date=start_date + timedelta(days=total_days - 1),
        status="active",
        entry_state=entry_state,
    )
    session.add(block)
    await session.flush()
    return block


async def roll_over_if_complete(
    session: AsyncSession, app_user_id: int, today: date
) -> Optional[TrainingBlock]:
    """Закрывает активный блок, чей planned_end_date уже прошёл, и тут же
    открывает следующий по ТОМУ ЖЕ шаблону (те же фазы, тот же сплит, тот же
    мезо/микроцикл — только снимок состояния переносится вперёд).

    ПОЧЕМУ переход автоматический, а не предложение пользователю (P0-08,
    ревью Задачи 7, Находка 1): ensure_active_block раньше безусловно
    возвращала уже истёкший блок, а SchedulingEngine.ensure_horizon достраивала
    календарь только ДО его planned_end_date — как только сегодняшняя дата
    проходила эту границу, достраивалось ноль дней, и пользователь открывал
    приложение с пустым календарём. Модель "предлагаем, а не решаем" при этом
    не нарушается: следующий блок — тот же самый шаблон, что и закрытый, то
    есть само решение об этом шаблоне пользователь уже утвердил раньше, когда
    настраивал периодизацию. Карточка итогов (Задача 9) по-прежнему разбирает
    прошедший блок и предлагает структурные правки — но не то, быть ли
    следующему блоку вообще.

    Перекатывает РОВНО НА ОДИН ШАГ. Цепочку из нескольких просроченных блоков
    подряд (человек вернулся после отпуска длиной в несколько блоков) в цикл
    собирает вызывающая сторона — см. ensure_active_block и
    params.MAX_CHAIN_ROLLOVERS.

    Коммитит сессию (как и ensure_active_block, см. предупреждение там) —
    вызывающая сторона не должна накопить в этой сессии собственные
    незакоммиченные изменения раньше этого вызова.
    """
    block = await get_active_block(session, app_user_id)
    if block is None:
        return None
    if today <= block.planned_end_date:
        return None

    # Снимок состояния на выход берём по упражнениям, тренированным ВНУТРИ
    # этого блока — так он честно отражает прогрессию именно за этот блок.
    # P0-08, повторное ревью Задачи 7, Находка 1: если блок прошёл целиком
    # без единой тренировки (типичный случай для промежуточных блоков
    # цепочки — их никто не тренировал, потому что человек был в отпуске),
    # снимать нечего: состояние не изменилось с момента входа в блок, а
    # build_state_snapshot делает отдельную загрузку истории на каждое
    # упражнение — на пустом блоке это лишние запросы на ровном месте.
    # Поэтому просто переносим entry_state закрываемого блока как есть,
    # вместо того чтобы (как раньше) искать fallback по недавним
    # упражнениям — тот fallback был честен для одиночного перехода, но
    # разоряется на цепочке из нескольких пустых блоков подряд.
    exercise_ids = await block_exercise_ids(session, app_user_id, block)
    if exercise_ids:
        exit_state = await build_state_snapshot(session, app_user_id, exercise_ids)
    else:
        exit_state = block.entry_state

    # P0-08, Задача 9, поправка 4: перезаписываем _chronic_level ТЕКУЩИМ
    # значением (а не переносим то, что было в entry_state закрываемого
    # блока) — это то же значение, которое станет entry_state[_chronic_level]
    # следующего блока НИЖЕ (тот же словарь передаётся как есть), поэтому
    # инвариант nxt.entry_state == prev.exit_state (см.
    # test_chain_rollover_catches_up_to_today) не нарушается.
    exit_state = {
        **(exit_state or {}),
        "_chronic_level": await _current_chronic_level(session, app_user_id, today),
    }

    block.status = "closed"
    block.close_reason = params.CLOSE_COMPLETED
    block.actual_end_date = block.planned_end_date
    block.exit_state = exit_state

    # СТРОГО planned_end_date + 1, а не today: иначе при заходе в приложение
    # спустя несколько дней после конца блока в календаре образовалась бы
    # дыра между старой границей и стартом нового блока. Долгий перерыв —
    # отдельный случай со своим правилом (params.MAX_CHAIN_ROLLOVERS выше).
    next_start = block.planned_end_date + timedelta(days=1)
    # P0-08, повторное ревью Задачи 7, Находка 4: арифметику planned_end_date
    # (и защиту от пустых фаз) не повторяем — переиспользуем create_block,
    # передав ей фазы закрываемого блока, а ссылки на мезо-/микроцикл и
    # сплит, которые create_block иначе взяла бы из ТЕКУЩИХ активных
    # настроек пользователя, переопределяем полями закрываемого блока СРАЗУ
    # после вызова: активные настройки на момент переката могли уже уйти
    # вперёд, а следующий блок цепочки обязан остаться на том же шаблоне,
    # что и закрытый.
    next_block = await create_block(
        session,
        app_user_id,
        next_start,
        block_index=block.block_index + 1,
        phases=phase_ops.from_json(block.phases),
        user_meso=None,
        user_micro=None,
        split_blueprint_id=block.split_blueprint_id,
        entry_state=exit_state,
    )
    next_block.user_mesocycle_id = block.user_mesocycle_id
    next_block.mesocycle_id = block.mesocycle_id
    next_block.user_microcycle_id = block.user_microcycle_id
    next_block.microcycle_length = block.microcycle_length
    await session.commit()
    # Возвращаем ЗАКРЫТЫЙ блок — он понадобится Задаче 9, чтобы по нему
    # создать карточку итогов.
    return block


async def _catch_up_active_block(
    session: AsyncSession, app_user_id: int, today: date
) -> None:
    """Перекатывает активный блок в цикле, пока он не покроет today, либо
    пока не исчерпан params.MAX_CHAIN_ROLLOVERS (P0-08, повторное ревью
    Задачи 7, Находка 1).

    Каждая итерация — ровно один шаг roll_over_if_complete: он сам
    останавливается (возвращает None), как только активного блока либо нет,
    либо он уже покрывает today. Если после MAX_CHAIN_ROLLOVERS шагов блок
    всё ещё не дотянул до today — прежняя программа, скорее всего, потеряла
    смысл (перерыв длиннее, чем несколько блоков подряд), и восстанавливать
    цепочку дальше бессмысленно: закрываем блок с причиной "layoff" и
    начинаем новый с сегодня, как и для самого первого блока пользователя.
    """
    rollovers = 0
    while await roll_over_if_complete(session, app_user_id, today) is not None:
        rollovers += 1
        if rollovers < params.MAX_CHAIN_ROLLOVERS:
            continue
        current = await get_active_block(session, app_user_id)
        if current is not None and today > current.planned_end_date:
            await _close_for_layoff(session, app_user_id, current, today)
        break


async def _close_for_layoff(
    session: AsyncSession, app_user_id: int, block: TrainingBlock, today: date
) -> TrainingBlock:
    """Закрывает блок, который params.MAX_CHAIN_ROLLOVERS перекатов того же
    шаблона так и не дотянули до today, и открывает новый со start_date =
    today (P0-08, повторное ревью Задачи 7, Находка 1).

    Снимок на выход — по тому же правилу, что и в roll_over_if_complete: раз
    блок дошёл до предела перекатов, тренировок в нём заведомо не было, брать
    состояние по recent_exercise_ids бессмысленно — переносим entry_state как
    есть.
    """
    exercise_ids = await block_exercise_ids(session, app_user_id, block)
    exit_state = (
        await build_state_snapshot(session, app_user_id, exercise_ids)
        if exercise_ids
        else block.entry_state
    )

    # P0-08, Задача 9, поправка 4 — то же самое, что и в roll_over_if_complete.
    exit_state = {
        **(exit_state or {}),
        "_chronic_level": await _current_chronic_level(session, app_user_id, today),
    }

    block.status = "closed"
    block.close_reason = params.CLOSE_LAYOFF
    block.actual_end_date = block.planned_end_date
    block.exit_state = exit_state

    # Как и в roll_over_if_complete (Находка 4) — переиспользуем create_block
    # вместо повторения арифметики planned_end_date, ссылки на шаблон
    # переопределяем полями закрываемого блока после вызова.
    next_block = await create_block(
        session,
        app_user_id,
        today,
        block_index=block.block_index + 1,
        phases=phase_ops.from_json(block.phases),
        user_meso=None,
        user_micro=None,
        split_blueprint_id=block.split_blueprint_id,
        entry_state=exit_state,
    )
    next_block.user_mesocycle_id = block.user_mesocycle_id
    next_block.mesocycle_id = block.mesocycle_id
    next_block.user_microcycle_id = block.user_microcycle_id
    next_block.microcycle_length = block.microcycle_length
    await session.commit()
    return next_block


async def ensure_active_block(
    session: AsyncSession, app_user_id: int, today: date
) -> Optional[TrainingBlock]:
    """Активный блок, создавая первый при необходимости.

    Прошлое не реконструируем: где проходили границы блоков раньше, достоверно
    вывести нельзя (формулы генератора и превью расходятся). Поэтому первый
    блок существующего пользователя начинается сегодня — разовый эффект
    перехода, зафиксированный в спеке §8.

    ВНИМАНИЕ: функция коммитит текущую транзакцию сессии (см. session.commit()
    ниже — как и SchedulingEngine.launch_and_unroll_plan, ensure_horizon, эта
    функция коммитит внутри себя, не только флашит). Вызывать её нужно ДО
    того, как вызывающий код начал накапливать собственные незакоммиченные
    изменения в этой сессии — иначе они уедут в БД вместе с блоком, задним
    числом и незапланированно.
    """
    # P0-08, ревью Задачи 7, Находка 1: сначала закрываем истёкший блок и
    # открываем следующий по тому же шаблону — иначе get_active_block ниже
    # нашёл бы блок, чей planned_end_date уже в прошлом, и все потребители
    # координаты (в первую очередь ensure_horizon) продолжали бы достраивать
    # календарь только до этой мёртвой границы. См. докстринг
    # roll_over_if_complete — почему переход именно автоматический.
    #
    # P0-08, повторное ревью Задачи 7, Находка 1: один перекат закрывает
    # разрыв ровно на длину одного блока. Человек, вернувшийся после отпуска
    # длиной в несколько блоков, при разовом перекате получил бы "активный"
    # блок, чей planned_end_date всё ещё в прошлом, — дефект пустого
    # календаря воспроизвёлся бы снова, просто на более редком сценарии.
    # _catch_up_active_block перекатывает в цикле, пока активный блок не
    # покроет today, либо пока не исчерпан params.MAX_CHAIN_ROLLOVERS.
    #
    # P0-08, повторное ревью Задачи 7, Находка 2: переход (как и создание
    # первого блока в ветке ниже) не атомарен — закрытие старого блока и
    # вставка следующего происходят раздельными операциями. Два конкурентных
    # запроса, пересекающих границу блока (мобильный клиент на старте дёргает
    # контекст дня и контекст периодизации одновременно), могут столкнуться
    # на той же гонке, что и ветка создания первого блока: проигравший
    # получает IntegrityError на uq_training_blocks_user_index. Обрабатываем
    # по тому же контракту.
    try:
        await _catch_up_active_block(session, app_user_id, today)
    except IntegrityError:
        await session.rollback()
        existing = await get_active_block(session, app_user_id)
        if existing is None:
            # Блока по-прежнему нет — IntegrityError был не про эту гонку,
            # а про что-то другое. Глотать причину в этом случае нельзя.
            raise
        return existing

    existing = await get_active_block(session, app_user_id)
    if existing is not None:
        return existing

    user_meso = (
        await session.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == app_user_id,
                AppUserMesocycle.is_active.is_(True),
            )
        )
    ).scalars().first()
    user_micro = (
        await session.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == app_user_id,
                AppUserMicrocycle.is_active.is_(True),
            )
        )
    ).scalars().first()

    if user_meso is None or user_micro is None:
        return None

    snapshot = await _snapshot_phases_from_template(
        session, user_meso, user_micro.length_days
    )
    if not snapshot:
        return None

    active_split = (
        await session.execute(
            select(UserSplit).where(
                UserSplit.app_user_id == app_user_id, UserSplit.is_active.is_(True)
            )
        )
    ).scalars().first()

    last_index = (
        await session.execute(
            select(TrainingBlock.block_index)
            .where(TrainingBlock.app_user_id == app_user_id)
            .order_by(TrainingBlock.block_index.desc())
            .limit(1)
        )
    ).scalars().first()

    # P0-08, ревью: снимок на входе в блок собирается по недавним
    # упражнениям, а не по всей истории — на каждое упражнение приходится
    # отдельная загрузка истории, а смысл снимка зафиксировать состояние
    # актуальных движений, а не тех, что человек делал год назад.
    exercise_ids = await recent_exercise_ids(
        session,
        app_user_id,
        since=today - timedelta(days=params.SNAPSHOT_WINDOW_DAYS),
    )
    # P0-08, Задача 9, поправка 4: первый блок пользователя тоже обязан
    # получить _chronic_level в entry_state — иначе перенос плановой
    # разгрузки (decide._postpone) на самом первом блоке не сработает
    # никогда, ровно как и на автопереходах, где та же поправка внесена в
    # roll_over_if_complete/_close_for_layoff.
    entry_state = {
        **(await build_state_snapshot(session, app_user_id, exercise_ids)),
        "_chronic_level": await _current_chronic_level(session, app_user_id, today),
    }
    try:
        block = await create_block(
            session,
            app_user_id,
            today,
            block_index=(last_index or 0) + 1,
            phases=snapshot,
            user_meso=user_meso,
            user_micro=user_micro,
            split_blueprint_id=active_split.blueprint_id if active_split else None,
            entry_state=entry_state,
        )
    except IntegrityError:
        # Гонка: схема "проверить — потом создать" не атомарна. Мобильный
        # клиент на старте дёргает контекст дня и контекст периодизации
        # одновременно — оба запроса могут увидеть "блока нет", оба посчитать
        # один и тот же block_index, и второй INSERT падает на уникальном
        # индексе uq_training_blocks_user_index. Это не баг индекса — это его
        # работа, гарантия целостности. Но ensure_* обязана ВЕРНУТЬ блок, а
        # не упасть, когда его только что создал соседний запрос: откатываем
        # свою неудачную вставку и забираем то, что вставил конкурент.
        await session.rollback()
        block = await get_active_block(session, app_user_id)
        if block is None:
            # Блока по-прежнему нет — IntegrityError был не про эту гонку
            # (иначе get_active_block нашёл бы конкурентский блок), а про
            # что-то другое. Глотать причину в этом случае нельзя.
            raise
        return block
    await session.commit()
    return block


async def recent_exercise_ids(
    session: AsyncSession, app_user_id: int, since: Optional[date] = None
) -> list[int]:
    """Упражнения, встречавшиеся в завершённых сессиях (опционально — с даты)."""
    stmt = (
        select(WorkoutSessionExercise.exercise_id)
        .join(
            WorkoutSession,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "finished",
        )
        .distinct()
    )
    if since is not None:
        stmt = stmt.where(WorkoutSession.finished_at >= since)
    return [row for row in (await session.execute(stmt)).scalars().all() if row]


async def block_exercise_ids(
    session: AsyncSession, app_user_id: int, block: TrainingBlock
) -> list[int]:
    """Упражнения, которые тренировались внутри этого блока."""
    stmt = (
        select(WorkoutSessionExercise.exercise_id)
        .join(
            WorkoutSession,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "finished",
            WorkoutSession.training_block_id == block.id,
        )
        .distinct()
    )
    return [row for row in (await session.execute(stmt)).scalars().all() if row]


async def build_state_snapshot(
    session: AsyncSession, app_user_id: int, exercise_ids: list[int]
) -> dict[str, dict]:
    """Состояние прогрессии по упражнениям на текущий момент.

    Ключ — строка: JSONB не умеет целочисленные ключи, а обратно мы читаем
    только для показа и сравнения снимков.
    """
    snapshot: dict[str, dict] = {}
    for exercise_id in exercise_ids:
        history = await load_history(session, app_user_id, exercise_id)
        if not history.sessions:
            continue
        # Заглушка шага округления: step_kg((), "kg", None) всегда даёт 2.5 кг,
        # не глядя на реальное оборудование упражнения и настройки
        # пользователя. Безвредно СЕГОДНЯ — единственное поле ProgressionState,
        # зависящее от шага, это consecutive_misses, а оно в снимок ниже не
        # попадает (см. dict). Если consecutive_misses когда-нибудь добавят в
        # snapshot — придётся резолвить настоящий шаг по оборудованию
        # exercise_id (как это делает build_context/refresh_state), а не
        # хардкодить его здесь.
        state = rebuild_state(history, step_kg((), "kg", None))
        snapshot[str(exercise_id)] = {
            "working_e1rm": state.working_e1rm,
            "training_max": state.training_max,
            "best_e1rm_ever": state.best_e1rm_ever,
            "last_top_weight": state.last_top_weight,
            "last_scheme": state.last_scheme,
            "stalled": state.stalled,
            "completed_sessions": state.completed_sessions,
        }
    return snapshot


async def collect_decision_input(
    session: AsyncSession, app_user_id: int, block: TrainingBlock, today: date
):
    """Все входы решателя одним вызовом (P0-08, Задача 9).

    Вызывается и по активному блоку (обычный путь), и по только что
    закрытому (карточка итогов, см. service.refresh_proposals и поправку 1
    брифа) — координата блока целиком определяет, какая ветка decide()
    сработает: BlockState закрытого блока даёт position(...).is_complete=True,
    потому что today уже позже его planned_end_date.
    """
    from datetime import datetime, time, timedelta as _timedelta, timezone

    from api.services.fatigue.service import compute_readiness
    from api.services.models import PeriodizationProposal, UserCalendarDay, UserObservation
    from api.services.periodization.position import position
    from api.services.periodization.types import (
        DecisionInput,
        FatigueSignal,
        PlateauSignal,
        ReadinessSignal,
    )
    from api.services.readiness.repository import load_signals
    from api.services.readiness.verdict import build_verdict

    state = block_state(block)
    pos = position(state, today)

    # --- Усталость. Считаем ЗАДОМ НАПЕРЁД и выходим на первом не-fatigued
    # дне: у большинства пользователей это ровно один запрос к compute_readiness
    # (тяжёлому — он тянет все подходы за окно), а полная серия из
    # FATIGUED_DAYS_FOR_DELOAD вызовов нужна лишь тем, кто реально в яме
    # (все проверенные дни подряд оказались fatigued).
    fatigued_days = 0
    sharp_rise = False
    band_known = False
    chronic_level = None
    for offset in range(params.FATIGUED_DAYS_FOR_DELOAD):
        moment = datetime.combine(today, time(12, 0), tzinfo=timezone.utc) - _timedelta(days=offset)
        report = await compute_readiness(session, app_user_id, now=moment)
        if offset == 0:
            sharp_rise = report.progression.flag == "sharp_rise"
            chronic_level = report.progression.chronic_level
            band_known = report.systemic.band != "unknown"
        if report.systemic.band != "fatigued":
            break
        fatigued_days += 1

    baseline = None
    if block.entry_state:
        baseline = block.entry_state.get("_chronic_level")

    # --- Плато по упражнениям блока.
    exercise_ids = await block_exercise_ids(session, app_user_id, block)
    with_history = 0
    stalled = 0
    # P0-08, ревью Задачи 5, Находка: PlateauSignal.stalled_after_deload не
    # имеет инварианта уникальности на уровне типа — дубликат exercise_id дал
    # бы два одинаковых структурных предложения на одно и то же упражнение
    # (_boundary_proposals в decide.py создаёт по одному Proposal на КАЖДЫЙ
    # элемент этого кортежа, без собственной дедупликации). exercise_ids уже
    # приходит из block_exercise_ids с .distinct() в SQL, так что дубликатов
    # структурно быть не должно — но продюсер этого поля именно эта функция,
    # поэтому дедупликация зафиксирована здесь явно, а не оставлена на
    # честное слово вызывающей стороны запроса.
    seen_exercise_ids: set[int] = set()
    stalled_after_deload: list[int] = []
    for exercise_id in exercise_ids:
        history = await load_history(session, app_user_id, exercise_id)
        st = rebuild_state(history, step_kg((), "kg", None))
        if st.completed_sessions < 6:
            continue
        with_history += 1
        if not st.stalled:
            continue
        stalled += 1
        # Критерий структурного предложения: разгрузка уже была и не помогла.
        if any(s.is_deload for s in history.sessions) and exercise_id not in seen_exercise_ids:
            seen_exercise_ids.add(exercise_id)
            stalled_after_deload.append(exercise_id)

    # --- Последние вердикты чек-ина.
    # Не SELECT DISTINCT + ORDER BY observed_at: Postgres запрещает
    # сортировку по колонке, не входящей в список DISTINCT ("SELECT DISTINCT
    # ON expressions must match initial ORDER BY expressions" —
    # InvalidColumnReferenceError). Один client_uuid обычно даёт несколько
    # строк (sleep, stress, soreness, pain — каждая своей строкой), поэтому
    # группируем по client_uuid и сортируем по САМОЙ ПОЗДНЕЙ отметке внутри
    # группы.
    uuids = (
        await session.execute(
            select(UserObservation.client_uuid)
            .where(
                UserObservation.app_user_id == app_user_id,
                UserObservation.client_uuid.isnot(None),
            )
            .group_by(UserObservation.client_uuid)
            .order_by(func.max(UserObservation.observed_at).desc())
            .limit(params.READINESS_LIMIT_WINDOW)
        )
    ).scalars().all()
    levels: list[str] = []
    for client_uuid in uuids:
        verdict = build_verdict(await load_signals(session, app_user_id, client_uuid))
        if verdict is not None:
            levels.append(verdict.level)

    # --- Тренировок до плановой разгрузки.
    workouts_to_deload = None
    if pos.days_to_deload is not None:
        deload_start = today + _timedelta(days=pos.days_to_deload)
        workouts_to_deload = len(
            (
                await session.execute(
                    select(UserCalendarDay.id).where(
                        UserCalendarDay.app_user_id == app_user_id,
                        UserCalendarDay.target_date >= today,
                        UserCalendarDay.target_date < deload_start,
                        UserCalendarDay.is_rest_day.is_(False),
                        UserCalendarDay.is_blackout.is_(False),
                    )
                )
            ).scalars().all()
        )

    used = (
        await session.execute(
            select(PeriodizationProposal.id).where(
                PeriodizationProposal.block_id == block.id,
                PeriodizationProposal.kind == params.KIND_EARLY_DELOAD,
                PeriodizationProposal.status == params.STATUS_ACCEPTED,
            ).limit(1)
        )
    ).scalars().first()

    return DecisionInput(
        position=pos,
        fatigue=FatigueSignal(
            fatigued_days=fatigued_days,
            sharp_rise=sharp_rise,
            band_known=band_known,
            chronic_level=chronic_level,
            chronic_at_block_start=baseline,
        ),
        plateau=PlateauSignal(
            exercises_with_history=with_history,
            stalled=stalled,
            stalled_after_deload=tuple(stalled_after_deload),
        ),
        readiness=ReadinessSignal(recent_levels=tuple(levels)),
        early_deload_used=used is not None,
        workouts_to_planned_deload=workouts_to_deload,
    )
