"""Расчёт метрик отчёта по произвольному диапазону дат.

Один и тот же код обслуживает неделю, месяц и год — различается только
диапазон. Никаких ленивых загрузок ORM: все данные берутся явными запросами,
чтобы метрики тестировались таблицей кейсов.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import (
    Exercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
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
    """Отбор по ДАТЕ СТАРТА сессии — как в volume_repo.performed_for."""
    return (
        WorkoutSession.app_user_id == app_user_id,
        WorkoutSession.status == "finished",
        WorkoutSession.finished_at.is_not(None),
        func.date(WorkoutSession.started_at) >= start,
        func.date(WorkoutSession.started_at) <= end,
    )


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
    performed = await volume_repo.performed_for(session, app_user_id, window)

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
