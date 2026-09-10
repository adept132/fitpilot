"""Расчёт метрик отчёта по произвольному диапазону дат.

Один и тот же код обслуживает неделю, месяц и год — различается только
диапазон. Никаких ленивых загрузок ORM: все данные берутся явными запросами,
чтобы метрики тестировались таблицей кейсов.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import (
    Exercise,
    UserRecord,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.exercise_localization import localized_names
from api.services.progression.metrics import effort_to_rir
from api.services.volume import repository as volume_repo
from api.services.volume.landmarks import landmarks_for

# Фильтр допуска рабочего подхода. Совпадает с /progress/achievements: один
# жим «500 кг» иначе отравляет отчёт целого месяца.
def _work_set_filters():
    return (
        WorkoutSessionSet.is_completed.is_(True),
        WorkoutSessionSet.is_anomalous.is_(False),
        WorkoutSessionSet.set_type == "normal",
        WorkoutSessionSet.weight.is_not(None),
        WorkoutSessionSet.reps.is_not(None),
        WorkoutSessionSet.weight > 0,
        WorkoutSessionSet.reps > 0,
    )


def _finished_sessions_in(app_user_id: int, start: date, end: date):
    """Отбор по ДАТЕ СТАРТА сессии — как в volume_repo.performed_for.

    Часовой пояс: func.date() разворачивает started_at в TIMEZONE сервера
    Postgres (UTC), а границы start/end приходят от вызывающего кода как
    ЛОКАЛЬНЫЕ даты пользователя. Для пользователя, чей часовой пояс далёк
    от UTC, тренировка в первый или последний локальный день периода может
    попасть в дату соседнего периода по UTC-сдвигу.

    Это НЕ решается здесь намеренно: соглашение унаследовано от
    volume_repo.performed_for (см. её докстринг) и держит отчёт
    согласованным с остальной кодовой базой, а не только внутри себя.
    Аккуратный часовой-пояс-осведомлённый фикс потянул бы за собой общее
    поведение далеко за пределы отчётов. Для ЖИВОГО окна объёма пересчёт
    при следующем открытии экрана сгладил бы редкую пограничную ошибку, но
    отчёт — иммутабельный снапшот: записанное один раз уже никогда не
    пересчитается, поэтому расхождение стоит держать на виду, а не
    молчаливым."""
    return (
        WorkoutSession.app_user_id == app_user_id,
        WorkoutSession.status == "finished",
        WorkoutSession.finished_at.is_not(None),
        func.date(WorkoutSession.started_at) >= start,
        func.date(WorkoutSession.started_at) <= end,
    )


# Подход считается тяжёлым при пяти и менее повторах — граница силового
# диапазона, принятая в движке прогрессии.
HEAVY_REPS_MAX = 5

# Сколько недель истории берём под базу e1RM. Восемь недель — компромисс:
# достаточно, чтобы база нашлась у редко тренирующегося, и мало, чтобы
# устаревший результат не занижал интенсивность.
BASELINE_WEEKS = 8


@dataclass(frozen=True)
class AdherenceMetric:
    planned_days: int
    completed_days: int
    missed_days: int
    rate: float


@dataclass(frozen=True)
class MuscleVolume:
    direct: float
    indirect: float
    mev: int
    mav: int
    mrv: int


@dataclass(frozen=True)
class VolumeMetric:
    work_sets: int
    tonnage_kg: float
    by_muscle: dict[str, MuscleVolume] = field(default_factory=dict)


@dataclass(frozen=True)
class TimeMetric:
    sessions: int
    total_minutes: int
    avg_session_minutes: float | None
    sets_per_hour: float | None


@dataclass(frozen=True)
class IntensityMetric:
    avg_relative: float | None
    heavy_set_share: float | None


@dataclass(frozen=True)
class EffortMetric:
    avg_rir: float | None
    labeled_share: float


@dataclass(frozen=True)
class RecordItem:
    exercise_id: int | None
    exercise_name: str
    record_type: str
    value: float
    achieved_on: date
    localized_names: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReportMetrics:
    period_type: str
    period_start: date
    period_end: date
    adherence: AdherenceMetric
    volume: VolumeMetric
    intensity: IntensityMetric
    effort: EffortMetric
    time: TimeMetric
    records: list[RecordItem] = field(default_factory=list)


async def compute_adherence_metric(
    session: AsyncSession, app_user_id: int, start: date, end: date
) -> AdherenceMetric:
    adherence = await volume_repo.adherence_for_range(session, app_user_id, start, end)
    rate = (
        adherence.completed_days / adherence.planned_days
        if adherence.planned_days
        else 0.0
    )
    return AdherenceMetric(
        planned_days=adherence.planned_days,
        completed_days=adherence.completed_days,
        missed_days=adherence.missed_days,
        rate=rate,
    )


async def compute_volume_metric(
    session: AsyncSession, app_user_id: int, start: date, end: date,
    level: str | None,
) -> VolumeMetric:
    """Объём периода: физические рабочие подходы, тоннаж и разбивка по мышцам.

    Разбивка переиспользует volume_repo.performed_for — окно там нужно только
    ради пары дат, поэтому конструируем синтетическое.

    У `work_sets`/`tonnage_kg` и `by_muscle` НАМЕРЕННО разные фильтры
    подходов — это две разные величины, а не одна и та же, посчитанная
    дважды:

    - `work_sets`/`tonnage_kg` — физические рабочие подходы: _work_set_filters()
      (is_completed, not is_anomalous, set_type == "normal", weight > 0,
      reps > 0) поверх _finished_sessions_in(). Дроп-сеты и подходы с
      нулевым/пустым весом или повторами сюда не идут — это не полноценный
      рабочий подход.
    - `by_muscle` — стимул для мышцы: performed_for() с
      require_finished_session=True. Условие завершённости сессии здесь то
      же самое (иначе подходы ещё активной тренировки просачивались бы в
      закрытый период мимо headline-цифр), но состав подходов шире —
      set_type IN ('normal', 'drop') без проверки веса/повторов. Дроп-сет
      физически не независимый рабочий подход (поэтому не считается в
      work_sets), но нагрузку на мышцу он даёт и обязан идти в объём — так
      же, как на живом экране объёма (volume_repo.performed_for). Делать
      by_muscle зеркалом work_sets значило бы разойтись с продуктовой
      моделью объёма ради фальшивой внутренней симметрии.
    """
    totals = (await session.execute(
        select(
            func.count(WorkoutSessionSet.id),
            func.coalesce(func.sum(WorkoutSessionSet.weight * WorkoutSessionSet.reps), 0),
        )
        .select_from(WorkoutSessionSet)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .join(WorkoutSession, WorkoutSessionExercise.workout_session_id == WorkoutSession.id)
        .where(*_finished_sessions_in(app_user_id, start, end), *_work_set_filters())
    )).one()

    window = volume_repo.Window(
        block_id=None, window_index=0, phase_number=None,
        start_date=start, end_date=end, is_deload=False,
    )
    performed = await volume_repo.performed_for(
        session, app_user_id, window, require_finished_session=True,
    )

    by_muscle: dict[str, MuscleVolume] = {}
    for muscle, contribution in performed.items():
        lm = landmarks_for(muscle, level)
        if lm is None:
            continue
        by_muscle[muscle] = MuscleVolume(
            direct=contribution.direct, indirect=contribution.indirect,
            mev=lm.mev, mav=lm.mav, mrv=lm.mrv,
        )

    return VolumeMetric(
        work_sets=int(totals[0]),
        tonnage_kg=float(totals[1]),
        by_muscle=by_muscle,
    )


async def compute_time_metric(
    session: AsyncSession, app_user_id: int, start: date, end: date
) -> TimeMetric:
    rows = (await session.execute(
        select(WorkoutSession.id, WorkoutSession.started_at, WorkoutSession.finished_at)
        .where(*_finished_sessions_in(app_user_id, start, end))
    )).all()
    if not rows:
        return TimeMetric(sessions=0, total_minutes=0, avg_session_minutes=None,
                          sets_per_hour=None)

    total_minutes = sum(
        max(0, int((finished - started).total_seconds() // 60))
        for _, started, finished in rows
    )
    work_sets = (await session.execute(
        select(func.count(WorkoutSessionSet.id))
        .select_from(WorkoutSessionSet)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .where(
            WorkoutSessionExercise.workout_session_id.in_([row[0] for row in rows]),
            *_work_set_filters(),
        )
    )).scalar_one()

    return TimeMetric(
        sessions=len(rows),
        total_minutes=total_minutes,
        avg_session_minutes=total_minutes / len(rows),
        sets_per_hour=(work_sets * 60 / total_minutes) if total_minutes else None,
    )


def _e1rm(weight: float, reps: int) -> float | None:
    """Бржицки. Не определена при 37 повторах и отрицательна выше."""
    if reps >= 37:
        return None
    return weight * 36.0 / (37.0 - reps)


async def compute_effort_metric(
    session: AsyncSession, app_user_id: int, start: date, end: date
) -> EffortMetric:
    rows = (await session.execute(
        select(WorkoutSessionSet.effort_level)
        .select_from(WorkoutSessionSet)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .join(WorkoutSession, WorkoutSessionExercise.workout_session_id == WorkoutSession.id)
        .where(*_finished_sessions_in(app_user_id, start, end), *_work_set_filters())
    )).scalars().all()
    if not rows:
        return EffortMetric(avg_rir=None, labeled_share=0.0)

    labeled = [value for value in rows if value]
    avg_rir = (
        sum(effort_to_rir(value) for value in labeled) / len(labeled) if labeled else None
    )
    return EffortMetric(avg_rir=avg_rir, labeled_share=len(labeled) / len(rows))


async def compute_intensity_metric(
    session: AsyncSession, app_user_id: int, start: date, end: date,
    baseline_start: date,
) -> IntensityMetric:
    """Относительная интенсивность: вес подхода к e1RM ДО периода.

    Упражнения без базы в среднее не попадают — делить не на что, а
    подставлять текущий e1RM значило бы занижать прошлые периоды ровно на
    величину случившегося с тех пор прогресса.
    """
    baseline_rows = (await session.execute(
        select(
            WorkoutSessionExercise.exercise_id,
            WorkoutSessionSet.weight,
            WorkoutSessionSet.reps,
        )
        .select_from(WorkoutSessionSet)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .join(WorkoutSession, WorkoutSessionExercise.workout_session_id == WorkoutSession.id)
        .where(
            *_finished_sessions_in(app_user_id, baseline_start, start - timedelta(days=1)),
            *_work_set_filters(),
        )
    )).all()

    baseline: dict[int, float] = {}
    for exercise_id, weight, reps in baseline_rows:
        value = _e1rm(float(weight), int(reps))
        if value is not None and value > baseline.get(exercise_id, 0.0):
            baseline[exercise_id] = value

    period_rows = (await session.execute(
        select(
            WorkoutSessionExercise.exercise_id,
            WorkoutSessionSet.weight,
            WorkoutSessionSet.reps,
        )
        .select_from(WorkoutSessionSet)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .join(WorkoutSession, WorkoutSessionExercise.workout_session_id == WorkoutSession.id)
        .where(*_finished_sessions_in(app_user_id, start, end), *_work_set_filters())
    )).all()
    if not period_rows:
        return IntensityMetric(avg_relative=None, heavy_set_share=None)

    ratios = [
        float(weight) / baseline[exercise_id]
        for exercise_id, weight, _ in period_rows
        if baseline.get(exercise_id)
    ]
    heavy = sum(1 for _, _, reps in period_rows if int(reps) <= HEAVY_REPS_MAX)

    return IntensityMetric(
        avg_relative=(sum(ratios) / len(ratios)) if ratios else None,
        heavy_set_share=heavy / len(period_rows),
    )


async def collect_records(
    session: AsyncSession, app_user_id: int, start: date, end: date
) -> list[RecordItem]:
    rows = (await session.execute(
        select(UserRecord)
        .where(
            UserRecord.app_user_id == app_user_id,
            UserRecord.date_achieved >= start,
            UserRecord.date_achieved <= end,
        )
        .order_by(UserRecord.date_achieved, UserRecord.id)
    )).scalars().all()
    exercise_ids = {row.exercise_id for row in rows if row.exercise_id is not None}
    exercises = (
        await session.execute(select(Exercise).where(Exercise.id.in_(exercise_ids)))
    ).scalars().all() if exercise_ids else []
    exercises_by_id = {exercise.id: exercise for exercise in exercises}
    return [
        RecordItem(
            exercise_id=row.exercise_id,
            exercise_name=row.exercise_name,
            record_type=row.record_type,
            value=float(row.value),
            achieved_on=row.date_achieved,
            localized_names=(
                localized_names(exercises_by_id[row.exercise_id])
                if row.exercise_id in exercises_by_id
                else {"ru": row.exercise_name}
            ),
        )
        for row in rows
    ]


async def compute_metrics(
    session: AsyncSession, app_user_id: int, period_type: str,
    start: date, end: date, level: str | None,
) -> ReportMetrics:
    baseline_start = start - timedelta(weeks=BASELINE_WEEKS)
    return ReportMetrics(
        period_type=period_type,
        period_start=start,
        period_end=end,
        adherence=await compute_adherence_metric(session, app_user_id, start, end),
        volume=await compute_volume_metric(session, app_user_id, start, end, level),
        intensity=await compute_intensity_metric(
            session, app_user_id, start, end, baseline_start
        ),
        effort=await compute_effort_metric(session, app_user_id, start, end),
        time=await compute_time_metric(session, app_user_id, start, end),
        records=await collect_records(session, app_user_id, start, end),
    )


def has_activity(metrics: ReportMetrics) -> bool:
    """Был ли период вообще прожит: хоть один плановый день или тренировка."""
    return metrics.adherence.planned_days > 0 or metrics.time.sessions > 0
