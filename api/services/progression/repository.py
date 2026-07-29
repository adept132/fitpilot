"""Единственное место в пакете, которое знает про SQLAlchemy.

Ядро движка работает с dataclasses и ничего не знает про БД. Здесь —
загрузка истории, сборка контекста, сохранение предписания и обновление
кэша состояния.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.progression import params
from api.services.progression.metrics import effort_to_rir
from api.services.progression.rounding import step_kg
from api.services.progression.state import rebuild_state
from api.services.progression.types import (
    ENGINE_VERSION,
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
)
from api.services.models import (
    AppUserMesocycle,
    AppUserProfile,
    Exercise,
    MesocyclePhase,
    UserExerciseProgressionState,
    WorkoutSession,
    WorkoutSessionExercise,
)

HISTORY_LIMIT = 12

# Effort_tier фазы, при котором сессия — разгрузочная (rebuild_state не
# засчитывает такие сессии в счёт застоя). Строковый литерал, а не число —
# под Global Constraint "магических чисел" не подпадает; тот же литерал уже
# используется в reduction.py (ctx.phase_effort_tier == "deload").
DELOAD_EFFORT_TIER = "deload"


def _phase_effort_tier_stmt(
    mesocycle_user_ids: Sequence[int], phase_number: Optional[int] = None
):
    """Общий JOIN-запрос AppUserMesocycle -> MesocyclePhase по номеру фазы.

    WorkoutSession.mesocycle_phase — это НОМЕР фазы (int), а не её название
    (см. api/services/models.py). Строковый effort_tier ("deload"/"easy"/...)
    лежит в MesocyclePhase и достаётся так:
    WorkoutSession.app_user_mesocycle_id -> AppUserMesocycle.mesocycle_id ->
    MesocyclePhase.mesocycle_id, MesocyclePhase.phase_number == workout.mesocycle_phase.

    Загрузка через selectinload/relationship тут не подходит: связь между
    WorkoutSession и конкретной MesocyclePhase — не FK, а совпадение
    (mesocycle_id, phase_number), поэтому это не путь релейшнов, а join по
    столбцам. Единственное место, которое строит этот JOIN (P0-06 C2 —
    ранее он был продублирован в _load_deload_map и в
    workouts.py::get_exercise_autoprogression); обе точки теперь используют
    эту функцию — напрямую (_load_deload_map, батчем) или через
    resolve_phase_effort_tier (одна пара, все пишущие пути).
    """
    stmt = (
        select(
            AppUserMesocycle.id,
            MesocyclePhase.phase_number,
            MesocyclePhase.effort_tier,
        )
        .join(
            MesocyclePhase,
            MesocyclePhase.mesocycle_id == AppUserMesocycle.mesocycle_id,
        )
        .where(AppUserMesocycle.id.in_(mesocycle_user_ids))
    )
    if phase_number is not None:
        stmt = stmt.where(MesocyclePhase.phase_number == phase_number)
    return stmt


async def _load_deload_map(
    session: AsyncSession, workouts: Sequence[WorkoutSession]
) -> dict[tuple[int, int], str]:
    """effort_tier фазы мезоцикла для каждой сессии истории — ОДНИМ запросом.

    Батчим одним доп. запросом по всем сессиям истории сразу — иначе на
    каждую из HISTORY_LIMIT сессий пришлось бы делать свой join (N+1
    запросов на каждую загрузку истории).
    """
    pairs = {
        (w.app_user_mesocycle_id, w.mesocycle_phase)
        for w in workouts
        if w.app_user_mesocycle_id is not None and w.mesocycle_phase is not None
    }
    if not pairs:
        return {}

    mesocycle_user_ids = {p[0] for p in pairs}
    stmt = _phase_effort_tier_stmt(mesocycle_user_ids)
    rows = (await session.execute(stmt)).all()
    return {
        (app_user_mesocycle_id, phase_number): effort_tier
        for app_user_mesocycle_id, phase_number, effort_tier in rows
    }


async def resolve_phase_effort_tier(
    session: AsyncSession,
    app_user_mesocycle_id: Optional[int],
    mesocycle_phase: Optional[int],
    default: str = "medium",
) -> str:
    """effort_tier ТЕКУЩЕЙ фазы мезоцикла по (app_user_mesocycle_id, phase_number).

    Единая точка резолва фазы для всех пишущих путей движка (P0-06 C2):
    добавление упражнения, завершение сессии, создание сессии из плана.
    Без неё build_context(..., phase_effort_tier=...) получает дефолт
    "medium" на каждом пишущем пути, и правило deload_phase (reduction.py) и
    слой 2 resolve_scheme (силовая фаза -> percent_1rm) — мёртвый код,
    несмотря на то что read-only /autoprogression считает по настоящей фазе.

    default="medium" — тот же дефолт, что был у build_context(phase_effort_tier)
    и раньше жил в вызывающем коде по всей кодовой базе; здесь он один.
    """
    if app_user_mesocycle_id is None or mesocycle_phase is None:
        return default
    stmt = _phase_effort_tier_stmt([app_user_mesocycle_id], phase_number=mesocycle_phase)
    row = (await session.execute(stmt)).first()
    return row.effort_tier if row is not None else default


async def load_history(
    session: AsyncSession,
    app_user_id: int,
    exercise_id: int,
    limit: int = HISTORY_LIMIT,
) -> ExerciseHistory:
    """Последние завершённые сессии с упражнением, от новой к старой."""
    stmt = (
        select(WorkoutSession)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "finished",
            WorkoutSessionExercise.exercise_id == exercise_id,
        )
        .options(
            selectinload(WorkoutSession.exercises).selectinload(
                WorkoutSessionExercise.sets
            )
        )
        .order_by(WorkoutSession.finished_at.desc())
        .limit(limit)
    )
    workouts = (await session.execute(stmt)).scalars().unique().all()
    deload_map = await _load_deload_map(session, workouts)

    sessions: list[SessionFact] = []
    for workout in workouts:
        se = next(
            (e for e in workout.exercises if e.exercise_id == exercise_id), None
        )
        if se is None:
            continue

        facts = tuple(
            SetFact(
                set_number=s.set_number,
                weight_kg=None if s.weight is None else float(s.weight),
                reps=0 if s.reps is None else int(s.reps),
                rir=effort_to_rir(s.effort_level),
                set_type=s.set_type or "normal",
                is_anomalous=bool(s.is_anomalous),
            )
            for s in se.sets
            if s.is_completed and s.parent_set_id is None
        )

        # P0-06 C3: se.prescription — JSONB, записанный write-once, без
        # схемы на границе ДО этого фикса (см. SyncPrescriptionSnapshot в
        # api/schemas/sync.py — валидация теперь есть на входе синка, но
        # это не защищает от строк, уже осевших в БД раньше, от прямых
        # правок в консоли и т.п.). from_dict() читает обязательные ключи
        # без .get() и падает KeyError на первом же мусоре — один такой
        # session_exercise делает загрузку истории (а с ней —
        # добавление упражнения, завершение тренировки, автопрогрессию)
        # невозможной для ВСЕХ сессий с этим упражнением сразу, и write-once
        # не даёт это исправить перезаписью. Разбор — не молчаливое glotanie:
        # решение — деградировать до "у сессии нет предписания", это тот же
        # путь, что и легитимный se.prescription is None (движок уйдёт в
        # бутстрап e1rm_factor), а не 500 на каждый запрос с этим упражнением.
        try:
            prescription = (
                Prescription.from_dict(se.prescription) if se.prescription else None
            )
        except (KeyError, TypeError, ValueError):
            prescription = None
        effort_tier = deload_map.get(
            (workout.app_user_mesocycle_id, workout.mesocycle_phase)
        )
        sessions.append(
            SessionFact(
                session_id=workout.id,
                finished_at=workout.finished_at,
                prescription=prescription,
                sets=facts,
                is_deload=(effort_tier == DELOAD_EFFORT_TIER),
            )
        )

    return ExerciseHistory(exercise_id=exercise_id, sessions=tuple(sessions))


def _days_since(history: ExerciseHistory) -> Optional[int]:
    for s in history.sessions:
        if s.finished_at is None:
            continue
        finished = s.finished_at
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - finished).days
    return None


async def build_context(
    session: AsyncSession,
    session_exercise: WorkoutSessionExercise,
    app_user_id: int,
    experience_level: Optional[str],
    settings: Optional[dict[str, Any]],
    rep_range_source: str = params.REP_SOURCE_FALLBACK,
    phase_effort_tier: str = "medium",
) -> SchemeContext:
    """Собрать контекст движка из строк БД. Наружу — только dataclasses."""
    exercise = session_exercise.exercise
    history = await load_history(session, app_user_id, exercise.id)
    step = step_kg(
        getattr(exercise, "equipment_needed", None) or [],
        (settings or {}).get("weight_unit", "kg"),
        (settings or {}).get("weight_steps"),
    )

    rep_min = session_exercise.recommended_rep_min
    rep_max = session_exercise.recommended_rep_max
    if not rep_min or not rep_max:
        rep_min, rep_max = params.TIER_REP_FALLBACK.get(
            getattr(exercise, "fatigue_tier", 2), params.TIER_REP_FALLBACK[2]
        )
        rep_range_source = params.REP_SOURCE_FALLBACK

    return SchemeContext(
        history=history,
        state=rebuild_state(history, step),
        last_outcome=None,
        target_sets=session_exercise.target_sets or 3,
        rep_min=int(rep_min),
        rep_max=int(rep_max),
        rep_range_source=rep_range_source,
        target_rir=(
            session_exercise.recommended_rir
            if session_exercise.recommended_rir is not None
            else params.DEFAULT_RIR
        ),
        equipment=tuple(getattr(exercise, "equipment_needed", None) or []),
        unit=(settings or {}).get("weight_unit", "kg"),
        weight_steps=(settings or {}).get("weight_steps") or {},
        experience_level=experience_level or "beginner",
        fatigue_tier=getattr(exercise, "fatigue_tier", 2),
        main_muscle_group=getattr(exercise, "main_muscle_group", None),
        phase_effort_tier=phase_effort_tier,
        days_since_last_session=_days_since(history),
        settings=settings or {},
    )


def persist_prescription(
    session_exercise: WorkoutSessionExercise, prescription: Prescription
) -> None:
    """Сохранить предписание и его проекцию в плоские recommended_*.

    Write-once: непустое prescription не переписывается. Пользователь уже
    видел эту цель, и подменять её задним числом нельзя — иначе evaluate()
    сравнит факт с целью, которой человек не видел, и выставит недобор за
    невыполнение невидимого.
    """
    if session_exercise.prescription:
        return
    if not prescription.sets:
        return

    session_exercise.prescription = prescription.to_dict()

    # Проекция первого рабочего подхода — единственное место, которое пишет
    # в плоские recommended_*. Их читают fatigue/, csv_format.py и старые
    # клиенты; другой код их трогать не должен, иначе они разъедутся с
    # prescription.
    first = prescription.sets[0]
    session_exercise.recommended_weight = first.weight_kg
    session_exercise.recommended_rep_min = first.rep_min
    session_exercise.recommended_rep_max = first.rep_max
    session_exercise.recommended_rir = first.rir


async def _get_or_create_state(
    session: AsyncSession, app_user_id: int, exercise_id: int
) -> UserExerciseProgressionState:
    stmt = select(UserExerciseProgressionState).where(
        UserExerciseProgressionState.app_user_id == app_user_id,
        UserExerciseProgressionState.exercise_id == exercise_id,
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        row = UserExerciseProgressionState(
            app_user_id=app_user_id, exercise_id=exercise_id
        )
        session.add(row)
    return row


def _apply_state(row: UserExerciseProgressionState, state: ProgressionState) -> None:
    row.working_e1rm = state.working_e1rm
    row.training_max = state.training_max
    row.best_e1rm_ever = state.best_e1rm_ever
    row.consecutive_misses = state.consecutive_misses
    row.sessions_since_gain = state.sessions_since_gain
    row.last_top_weight = state.last_top_weight
    row.last_scheme = state.last_scheme
    row.engine_version = ENGINE_VERSION


async def _load_settings(session: AsyncSession, app_user_id: int) -> dict[str, Any]:
    """Настройки пользователя (единица веса, шаги оборудования) из профиля.

    Профиля может не быть (например, у только что созданного пользователя) —
    тогда пустой dict, rounding.step_kg сам подставит DEFAULT_WEIGHT_STEPS.
    """
    settings = (
        await session.execute(
            select(AppUserProfile.settings).where(
                AppUserProfile.app_user_id == app_user_id
            )
        )
    ).scalar_one_or_none()
    return settings or {}


async def refresh_state(
    session: AsyncSession,
    app_user_id: int,
    exercise_id: int,
    next_prescription: Optional[Prescription],
) -> None:
    """Пересчитать кэш состояния из истории и положить предварительное предписание.

    Шаг округления считается ОТ РЕАЛЬНОГО ОБОРУДОВАНИЯ упражнения и настроек
    пользователя через rounding.step_kg — точно так же, как это делает
    build_context. Захардкоженный шаг 2.5 кг магический и неверен для
    гантелей, блочных тренажёров (шаг в фунтах) и пользовательских
    weight_steps. У refresh_state нет доступа к WorkoutSessionExercise (он
    вызывается и вне контекста конкретной сессии — например, после отдельного
    пересчёта), поэтому exercise и settings подгружаются здесь же по
    app_user_id/exercise_id — ценой одного доп. SELECT на упражнение и одного
    на профиль, что несравнимо дешевле неверного шага округления.
    """
    exercise = await session.get(Exercise, exercise_id)
    settings = await _load_settings(session, app_user_id)
    step = step_kg(
        getattr(exercise, "equipment_needed", None) or [],
        settings.get("weight_unit", "kg"),
        settings.get("weight_steps"),
    )

    row = await _get_or_create_state(session, app_user_id, exercise_id)
    history = await load_history(session, app_user_id, exercise_id)
    _apply_state(row, rebuild_state(history, step))
    if next_prescription is not None:
        row.next_prescription = next_prescription.to_dict()


async def load_next_prescription(
    session: AsyncSession, app_user_id: int, exercise_id: int
) -> Optional[Prescription]:
    stmt = select(UserExerciseProgressionState).where(
        UserExerciseProgressionState.app_user_id == app_user_id,
        UserExerciseProgressionState.exercise_id == exercise_id,
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None or not row.next_prescription:
        return None
    return Prescription.from_dict(row.next_prescription)


async def debug_all_states(
    session: AsyncSession, app_user_id: int
) -> list[UserExerciseProgressionState]:
    """Только для тестов: все строки состояния пользователя."""
    stmt = select(UserExerciseProgressionState).where(
        UserExerciseProgressionState.app_user_id == app_user_id
    )
    return list((await session.execute(stmt)).scalars().all())
