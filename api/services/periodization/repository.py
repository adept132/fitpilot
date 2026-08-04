"""Единственное место периодизации, знающее про БД.

Ядро (position/phases/decide) остаётся чистым: здесь мы только достаём входы
и материализуем блок.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
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
            entry_state=await build_state_snapshot(session, app_user_id, exercise_ids),
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
