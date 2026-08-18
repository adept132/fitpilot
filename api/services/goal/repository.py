"""Сбор входа автопилота из БД (P0-12 §5).

Здесь и только здесь автопилот касается базы: ядро (simulate, decide)
остаётся чистым и потому целиком покрывается тестами без Postgres.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.goal.types import FutureSession
from api.services.models import (
    Exercise,
    UserCalendarDay,
    UserExerciseProgressionState,
    WorkoutPlanExercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)


async def future_sessions(
    session: AsyncSession,
    app_user_id: int,
    exercise_id: int,
    today: date,
    until: date,
) -> list[FutureSession]:
    """Будущие дни календаря, в плане которых стоит целевое упражнение.

    Строго ПОСЛЕ today: сегодняшний день может быть уже начат, и включать
    его в прогноз значит обещать рост от тренировки, которая идёт прямо
    сейчас.

    РАСХОЖДЕНИЕ С БРИФОМ: заготовка джойнила WorkoutPlanExercise к
    UserCalendarDay через промежуточную таблицу WorkoutPlan по
    WorkoutPlanExercise.workout_plan_id — такой колонки нет вовсе, реальное
    имя FK — WorkoutPlanExercise.plan_id (см. api/services/models.py).
    UserCalendarDay уже хранит plan_id напрямую, так что промежуточный джойн
    к WorkoutPlan не нужен — джойним WorkoutPlanExercise прямо по нему.
    """
    rows = (await session.execute(
        select(UserCalendarDay.target_date, UserCalendarDay.micro_tag,
               WorkoutPlanExercise.target_sets)
        .join(WorkoutPlanExercise, WorkoutPlanExercise.plan_id == UserCalendarDay.plan_id)
        .where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date > today,
            UserCalendarDay.target_date <= until,
            UserCalendarDay.status == "planned",
            UserCalendarDay.is_rest_day.is_(False),
            UserCalendarDay.is_blackout.is_(False),
            WorkoutPlanExercise.exercise_id == exercise_id,
        )
        .order_by(UserCalendarDay.target_date)
    )).all()

    return [
        FutureSession(
            date=row.target_date,
            phase_effort_tier=row.micro_tag or "medium",
            prescription_sets=int(row.target_sets or 0),
        )
        for row in rows
    ]


async def current_e1rm(
    session: AsyncSession, app_user_id: int, exercise_id: int
) -> Optional[float]:
    """Рабочий e1RM из кэша прогрессии — та же величина, что двигает движок."""
    row = (await session.execute(
        select(UserExerciseProgressionState.working_e1rm).where(
            UserExerciseProgressionState.app_user_id == app_user_id,
            UserExerciseProgressionState.exercise_id == exercise_id,
        )
    )).scalar_one_or_none()
    return float(row) if row is not None else None


async def lift_stats(
    session: AsyncSession, app_user_id: int, exercise_id: int
) -> tuple[int, float]:
    """Число завершённых сессий лифта и доля тех, где вес рос.

    «Успех» здесь — сессия, чей верхний рабочий вес не ниже предыдущей:
    полный вердикт progression.evaluate требует предписания, которого у
    исторических сессий может не быть вовсе.

    РАСХОЖДЕНИЕ С БРИФОМ: у WorkoutSessionSet нет колонки weight_kg (она
    называется weight) и нет булева is_warmup — тип подхода в set_type
    ("normal" | "warmup" | "drop"). Отсеиваем не-рабочие подходы тем же
    множеством типов, что и progression.state.rebuild_state
    (progression.params.IGNORED_SET_TYPES) — вместо того чтобы заводить
    здесь второй, рассинхронизирующийся список литералов.
    """
    from api.services.progression.params import IGNORED_SET_TYPES

    rows = (await session.execute(
        select(WorkoutSession.finished_at, func.max(WorkoutSessionSet.weight))
        .join(WorkoutSessionExercise,
              WorkoutSessionExercise.workout_session_id == WorkoutSession.id)
        .join(WorkoutSessionSet,
              WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id)
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "finished",
            WorkoutSessionExercise.exercise_id == exercise_id,
            WorkoutSessionSet.is_completed.is_(True),
            WorkoutSessionSet.parent_set_id.is_(None),
            WorkoutSessionSet.set_type.notin_(IGNORED_SET_TYPES),
        )
        .group_by(WorkoutSession.id, WorkoutSession.finished_at)
        .order_by(WorkoutSession.finished_at)
    )).all()

    weights = [float(w) for _, w in rows if w is not None]
    if len(weights) < 2:
        return len(weights), 0.0

    grew = sum(1 for a, b in zip(weights, weights[1:]) if b >= a)
    return len(weights), grew / (len(weights) - 1)


async def adherence_ratios(
    session: AsyncSession, app_user_id: int, limit: int = 4
) -> list[float]:
    """Доли выполненных дней по последним закрытым окнам P0-09, новые первыми.

    РАСХОЖДЕНИЕ С БРИФОМ: заготовка импортировала volume.service._adherence_ratio
    напрямую — приватный (с подчёркивания) символ чужого модуля, которого в
    этом проекте больше никто извне не импортирует. Формула тривиальна
    (доля выполненных дней окна, 1.0 при отсутствии предписанных дней) и не
    относится к границам объёма (тем landmarks/MRV, которые Global
    Constraint запрещает дублировать) — дублируем её здесь одной строкой, а
    не тянем приватное имя через границу пакета.
    """
    from api.services.volume.repository import closed_windows

    windows = await closed_windows(session, app_user_id, limit)
    ratios = []
    for w in windows:
        data = w.adherence or {}
        planned = int(data.get("planned_days") or 0)
        ratios.append(1.0 if planned <= 0 else int(data.get("completed_days") or 0) / planned)
    return ratios


async def headroom_sets(
    session: AsyncSession,
    app_user_id: int,
    exercise_id: int,
    level: Optional[str],
    today: date,
) -> int:
    """Сколько эффективных подходов на главную мышцу лифта ещё влезает до MRV.

    Автопилот ходит внутри границ движка объёма (спека, решение 11): рычаг,
    выносящий мышцу за верхний landmark, не должен предлагаться вовсе.
    Границы берём те же, что P0-09, и масштабируем той же формулой — иначе
    десятидневная цель судилась бы семидневным потолком.
    """
    from api.services.muscle_keys import key_for_muscle
    from api.services.volume import repository as volume_repository
    from api.services.volume.landmarks import landmarks_for, scale_landmarks

    muscle_raw = (await session.execute(
        select(Exercise.main_muscle_group).where(Exercise.id == exercise_id)
    )).scalar_one_or_none()
    muscle = key_for_muscle(muscle_raw)
    if muscle is None:
        return 0

    lm = landmarks_for(muscle, level)
    if lm is None:
        return 0

    window = await volume_repository.current_window(session, app_user_id, today)
    if window is None:
        return 0

    length = await volume_repository.block_microcycle_length(session, window.block_id)
    scaled = scale_landmarks(lm, length / 7.0)

    prescribed = await volume_repository.prescribed_for(session, app_user_id, window)
    contribution = prescribed.get(muscle)
    planned = contribution.effective if contribution is not None else 0.0
    return max(0, int(scaled.mrv - planned))


async def exercise_context(
    session: AsyncSession, app_user_id: int, exercise_id: int
) -> dict:
    """Схема, верх диапазона повторов, тяжесть базы и главная мышца.

    Схему берём из кэша состояния (last_scheme): это то, что движок реально
    применил в последний раз, а не то, что он выбрал бы в вакууме.
    """
    from api.services.equipment import BARBELL, SMITH, normalize_equipment_list
    from api.services.muscle_keys import key_for_muscle

    row = (await session.execute(
        select(Exercise.main_muscle_group, Exercise.fatigue_tier, Exercise.equipment_needed)
        .where(Exercise.id == exercise_id)
    )).first()
    if row is None:
        return {"scheme": "e1rm_factor", "rep_max": 8,
                "is_heavy_compound": False, "muscle": None}

    state = (await session.execute(
        select(UserExerciseProgressionState.last_scheme,
               UserExerciseProgressionState.next_prescription)
        .where(
            UserExerciseProgressionState.app_user_id == app_user_id,
            UserExerciseProgressionState.exercise_id == exercise_id,
        )
    )).first()

    rep_max = 8
    if state is not None and state.next_prescription:
        sets = (state.next_prescription or {}).get("sets") or []
        tops = [s.get("rep_max") or s.get("rep_min") for s in sets if s.get("rep_min")]
        if tops:
            rep_max = max(int(t) for t in tops if t)

    equipment = set(normalize_equipment_list(list(row.equipment_needed or [])))
    return {
        "scheme": (state.last_scheme if state is not None else None) or "e1rm_factor",
        "rep_max": rep_max,
        "is_heavy_compound": row.fatigue_tier == 1 and bool(equipment & {BARBELL, SMITH}),
        "muscle": key_for_muscle(row.main_muscle_group),
    }


def _bootstrap_history(
    exercise_id: int, working_e1rm: float, scheme: str, rep_min: int, rep_max: int
) -> ExerciseHistory:
    """Одна синтетическая сессия на working_e1rm — запасной путь 2 (см.
    докстринг scheme_context), когда у упражнения нет ни одной завершённой
    тренировки в БД. Повторяет tests/test_goal_simulate.py::_ctx: предписание
    и факт выполнены по верхней границе диапазона при RIR 2 — это даёт
    resolve_scheme настоящую (не пустую) историю предписаний и якорь веса,
    от которого прокрутка (simulate.run) реально двигается, а не застревает
    в бутстрапе e1rm_factor -> no_basis навсегда.
    """
    seed_prescription = Prescription(
        scheme=scheme,
        sets=tuple(
            SetPrescription(n, working_e1rm, rep_min, rep_max, 2, "normal")
            for n in range(1, 4)
        ),
        reason_code="progressed",
        reason_text="",
    )
    seed_facts = tuple(
        SetFact(set_number=n, weight_kg=working_e1rm, reps=rep_max, rir=2)
        for n in range(1, 4)
    )
    return ExerciseHistory(
        exercise_id=exercise_id,
        sessions=(
            SessionFact(
                session_id=-1,
                finished_at=datetime.now(timezone.utc),
                prescription=seed_prescription,
                sets=seed_facts,
            ),
        ),
    )


async def scheme_context(
    session: AsyncSession, app_user_id: int, exercise_id: int, profile
) -> Optional[SchemeContext]:
    """Стартовый контекст движка для прокрутки вперёд (simulate.run).

    КРИТИЧЕСКАЯ ПОПРАВКА К БРИФУ (см. отчёт Задачи 1 и докстринг
    api/services/goal/simulate.py): plan_exercise() ВСЕГДА пересчитывает
    SchemeContext.state и last_outcome сам, через
    rebuild_state(ctx.history, ...) и _latest_outcome(ctx, ...) — то, что
    сюда положено в history, он ЦЕЛИКОМ игнорирует состояние снаружи, но
    ЦЕЛИКОМ зависит от history. Пустая history (как было в заготовке брифа)
    даёт rebuild_state working_e1rm=None -> схема e1rm_factor уходит в
    no_basis навсегда, и ни одна будущая сессия в прокрутке не двигает вес.

    Путь 1 (предпочтительный, используется здесь): настоящая история из БД
    через progression.repository.load_history — тот же загрузчик, которым
    пользуется build_context движка при живой сессии; не заводим второй
    такой сборщик.
    Путь 2 (запасной): если у упражнения нет ни одной завершённой
    тренировки, load_history вернёт пустой ExerciseHistory — тогда
    синтезируем одну bootstrap-сессию из working_e1rm (см.
    _bootstrap_history), как это сделано в _ctx() из
    tests/test_goal_simulate.py.
    """
    from api.services.equipment import normalize_equipment_list
    from api.services.progression.repository import load_history

    working = await current_e1rm(session, app_user_id, exercise_id)
    if working is None or working <= 0:
        return None

    row = (await session.execute(
        select(Exercise.main_muscle_group, Exercise.fatigue_tier, Exercise.equipment_needed)
        .where(Exercise.id == exercise_id)
    )).first()
    if row is None:
        return None

    ctx_bits = await exercise_context(session, app_user_id, exercise_id)
    settings = (profile.settings or {}) if profile else {}
    rep_min = max(1, ctx_bits["rep_max"] - 3)

    history = await load_history(session, app_user_id, exercise_id)
    if not history.sessions:
        history = _bootstrap_history(
            exercise_id, working, ctx_bits["scheme"], rep_min, ctx_bits["rep_max"]
        )

    return SchemeContext(
        history=history,
        state=ProgressionState(working_e1rm=working, last_scheme=ctx_bits["scheme"]),
        last_outcome=None,
        target_sets=3,
        rep_min=rep_min,
        rep_max=ctx_bits["rep_max"],
        rep_range_source="state",
        target_rir=2,
        equipment=tuple(normalize_equipment_list(list(row.equipment_needed or []))),
        experience_level=(profile.experience_level if profile else None) or "beginner",
        fatigue_tier=row.fatigue_tier,
        main_muscle_group=row.main_muscle_group,
        settings=settings,
    )
