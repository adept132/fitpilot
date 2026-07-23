"""Единственное место усталостной модели, знающее про БД.

Ядро (core.py) остаётся чистым: здесь мы только достаём подходы, превращаем
их в импульсы и складываем результат в отчёт.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.autoprogression import effort_to_rir
from api.services.fatigue import core
from api.services.fatigue.params import DEFAULT_PARAMS, MODEL_VERSION, FatigueParams
from api.services.models import (
    Exercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)

CONFIDENCE_COLD_START = "cold_start"
CONFIDENCE_LOW = "low"
CONFIDENCE_NORMAL = "normal"

# Ниже этой доли размеченных усилием подходов сигнал считаем слабым: без
# effort_level EF вырождается в константу и нагрузка схлопывается в тоннаж.
LOW_CONFIDENCE_LABELED_PCT = 40.0


@dataclass(frozen=True)
class ReadinessReport:
    model_version: str
    computed_at: datetime
    confidence: str
    systemic: core.Readiness
    muscular: dict[str, core.Readiness]
    mechanical: core.Readiness
    progression: core.Progression
    effort_labeled_pct: float
    imported_pct: float


@dataclass
class _Loaded:
    impulses: list[core.SetImpulse] = field(default_factory=list)
    labeled: int = 0
    total: int = 0
    imported_sessions: int = 0
    total_sessions: int = 0


async def _load(
    db: AsyncSession, app_user_id: int, now: datetime, p: FatigueParams
) -> _Loaded:
    since = now - timedelta(days=p.window_days)

    stmt = (
        select(
            WorkoutSession.id,
            WorkoutSession.started_at,
            WorkoutSession.import_source,
            WorkoutSessionSet.weight,
            WorkoutSessionSet.reps,
            WorkoutSessionSet.effort_level,
            Exercise.main_muscle_group,
            Exercise.secondary_muscle_groups,
            Exercise.fatigue_tier,
        )
        .select_from(WorkoutSessionSet)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .join(
            WorkoutSession,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .join(Exercise, WorkoutSessionExercise.exercise_id == Exercise.id)
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.started_at >= since,
            WorkoutSessionSet.is_completed.is_(True),
            # Разминка и дропсеты в нагрузку не идут; аномалии — тем более.
            WorkoutSessionSet.set_type == "normal",
            WorkoutSessionSet.is_anomalous.is_(False),
            WorkoutSessionSet.weight.isnot(None),
            WorkoutSessionSet.reps.isnot(None),
        )
    )

    out = _Loaded()
    seen_sessions: dict[int, bool] = {}

    for row in (await db.execute(stmt)).all():
        out.total += 1
        if row.effort_level:
            out.labeled += 1
        seen_sessions[row.id] = bool(row.import_source)

        out.impulses.append(
            core.SetImpulse(
                at=row.started_at,
                weight_kg=float(row.weight),
                reps=int(row.reps),
                rir=effort_to_rir(row.effort_level),
                main_muscle=row.main_muscle_group,
                secondary_muscles=tuple(row.secondary_muscle_groups or ()),
                fatigue_tier=int(row.fatigue_tier or 2),
            )
        )

    out.total_sessions = len(seen_sessions)
    out.imported_sessions = sum(1 for v in seen_sessions.values() if v)
    return out


Event = tuple[datetime, float]


def _daily_series(
    events: list[Event], tau_hours: float, now: datetime, days: int
) -> list[float]:
    """Дневной ряд F_c за последние `days` дней, свежий день последний."""
    return [
        core.decay(events, tau_hours, now - timedelta(days=offset))
        for offset in range(days - 1, -1, -1)
    ]


def _daily_bins(events: list[Event], now: datetime, days: int) -> list[float]:
    """Суммарная нагрузка по календарным дням, свежий день последний."""
    bins = [0.0] * days
    for at, load in events:
        offset = (now.date() - at.date()).days
        if 0 <= offset < days:
            bins[days - 1 - offset] += load
    return bins


async def compute_readiness(
    db: AsyncSession,
    app_user_id: int,
    now: datetime | None = None,
    p: FatigueParams = DEFAULT_PARAMS,
) -> ReadinessReport:
    now = now or datetime.now(timezone.utc)
    loaded = await _load(db, app_user_id, now, p)

    # Раскладываем каждый импульс по отсекам РОВНО ОДИН РАЗ и дальше работаем
    # с готовыми событиями. Иначе split_to_compartments со сборкой словарей
    # пересчитывался бы на каждый день окна и на каждую мышцу.
    systemic_events: list[Event] = []
    mechanical_events: list[Event] = []
    muscular_events: dict[str, list[Event]] = {}

    for impulse in loaded.impulses:
        loads = core.split_to_compartments(impulse, p)
        systemic_events.append((impulse.at, loads.systemic))
        mechanical_events.append((impulse.at, loads.mechanical))
        for muscle, value in loads.muscular.items():
            muscular_events.setdefault(muscle, []).append((impulse.at, value))

    window = p.window_days
    systemic = core.readiness_z(
        _daily_series(systemic_events, p.tau_systemic_h, now, window), p
    )
    mechanical = core.readiness_z(
        _daily_series(mechanical_events, p.tau_mechanical_h, now, window), p
    )
    muscular = {
        muscle: core.readiness_z(
            _daily_series(events, p.tau_muscular_h, now, window), p
        )
        for muscle, events in sorted(muscular_events.items())
    }

    # Прогрессию считаем по механике: самый чувствительный к травмам канал.
    progression = core.ewma_progression(
        _daily_bins(mechanical_events, now, window), p
    )

    labeled_pct = (loaded.labeled / loaded.total * 100.0) if loaded.total else 0.0
    imported_pct = (
        loaded.imported_sessions / loaded.total_sessions * 100.0
        if loaded.total_sessions
        else 0.0
    )

    if loaded.total_sessions == 0:
        confidence = CONFIDENCE_COLD_START
    elif labeled_pct < LOW_CONFIDENCE_LABELED_PCT:
        confidence = CONFIDENCE_LOW
    else:
        confidence = CONFIDENCE_NORMAL

    if systemic.z is None:
        confidence = CONFIDENCE_COLD_START

    return ReadinessReport(
        model_version=MODEL_VERSION,
        computed_at=now,
        confidence=confidence,
        systemic=systemic,
        muscular=muscular,
        mechanical=mechanical,
        progression=progression,
        effort_labeled_pct=round(labeled_pct, 1),
        imported_pct=round(imported_pct, 1),
    )
