from datetime import date, datetime, timedelta, timezone

import pytest

from api.services.models import (
    UserCalendarDay,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.reports.metrics import (
    compute_adherence_metric,
    compute_time_metric,
    compute_volume_metric,
)

PERIOD_START = date(2026, 8, 3)
PERIOD_END = date(2026, 8, 9)


async def _session_with_sets(db, user_id, *, started: datetime, exercise_id: int,
                             sets: list[dict], finished_after_min: int = 60):
    workout = WorkoutSession(
        app_user_id=user_id, source="free", status="finished",
        started_at=started, finished_at=started + timedelta(minutes=finished_after_min),
    )
    db.add(workout)
    await db.flush()
    se = WorkoutSessionExercise(
        workout_session_id=workout.id, exercise_id=exercise_id, order_index=0,
    )
    db.add(se)
    await db.flush()
    for number, item in enumerate(sets, start=1):
        db.add(WorkoutSessionSet(
            workout_session_exercise_id=se.id, set_number=number,
            set_type=item.get("set_type", "normal"),
            weight=item.get("weight", 50.0), reps=item.get("reps", 10),
            effort_level=item.get("effort_level"),
            is_completed=True, is_anomalous=item.get("is_anomalous", False),
        ))
    await db.flush()
    return workout


@pytest.mark.asyncio
async def test_adherence_counts_only_working_days(db, test_user):
    for day, status, rest in [
        (date(2026, 8, 3), "completed", False),
        (date(2026, 8, 5), "missed", False),
        (date(2026, 8, 6), "planned", True),   # день отдыха в знаменатель не идёт
    ]:
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=day, status=status,
            is_rest_day=rest, is_blackout=False,
        ))
    await db.commit()

    metric = await compute_adherence_metric(db, test_user.id, PERIOD_START, PERIOD_END)

    assert (metric.planned_days, metric.completed_days, metric.missed_days) == (2, 1, 1)
    assert metric.rate == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_adherence_of_untouched_period_is_zero_not_none(db, test_user):
    """Нулевой adherence — самое ценное сообщение отчёта, прятать его нельзя."""
    metric = await compute_adherence_metric(db, test_user.id, PERIOD_START, PERIOD_END)
    assert (metric.planned_days, metric.rate) == (0, 0.0)


@pytest.mark.asyncio
async def test_volume_excludes_anomalous_and_warmup_sets(db, test_user, seeded_history):
    await _session_with_sets(
        db, test_user.id,
        started=datetime(2026, 8, 4, 10, tzinfo=timezone.utc),
        exercise_id=seeded_history.id,
        sets=[
            {"weight": 60.0, "reps": 8},
            {"weight": 60.0, "reps": 8},
            {"weight": 20.0, "reps": 15, "set_type": "warmup"},
            {"weight": 500.0, "reps": 8, "is_anomalous": True},
        ],
    )
    await db.commit()

    metric = await compute_volume_metric(db, test_user.id, PERIOD_START, PERIOD_END, None)

    assert metric.work_sets == 2
    assert metric.tonnage_kg == pytest.approx(960.0)  # 2 * 60 * 8


@pytest.mark.asyncio
async def test_time_metric_uses_session_duration(db, test_user, seeded_history):
    await _session_with_sets(
        db, test_user.id,
        started=datetime(2026, 8, 4, 10, tzinfo=timezone.utc),
        exercise_id=seeded_history.id,
        sets=[{"weight": 60.0, "reps": 8}] * 4,
        finished_after_min=60,
    )
    await db.commit()

    metric = await compute_time_metric(db, test_user.id, PERIOD_START, PERIOD_END)

    assert metric.sessions == 1
    assert metric.total_minutes == 60
    assert metric.avg_session_minutes == pytest.approx(60.0)
    assert metric.sets_per_hour == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_time_metric_ignores_unfinished_sessions(db, test_user, seeded_history):
    workout = WorkoutSession(
        app_user_id=test_user.id, source="free", status="active",
        started_at=datetime(2026, 8, 4, 10, tzinfo=timezone.utc), finished_at=None,
    )
    db.add(workout)
    await db.commit()

    metric = await compute_time_metric(db, test_user.id, PERIOD_START, PERIOD_END)

    assert metric.sessions == 0
    assert metric.avg_session_minutes is None
