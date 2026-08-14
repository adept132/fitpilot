from datetime import date, datetime, timedelta, timezone

import pytest

from api.services.models import (
    UserCalendarDay,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.reports.metrics import (
    collect_records,
    compute_adherence_metric,
    compute_effort_metric,
    compute_intensity_metric,
    compute_metrics,
    compute_time_metric,
    compute_volume_metric,
    has_activity,
)

PERIOD_START = date(2026, 8, 3)
PERIOD_END = date(2026, 8, 9)
BASELINE_START = date(2026, 6, 1)


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


@pytest.mark.asyncio
async def test_avg_rir_ignores_unlabelled_sets(db, test_user, seeded_history):
    """effort_to_rir(None) отдаёт DEFAULT_RIR = 2 — неразмеченные подходы
    обязаны выпадать из среднего, а не тихо голосовать двойкой."""
    await _session_with_sets(
        db, test_user.id,
        started=datetime(2026, 8, 4, 10, tzinfo=timezone.utc),
        exercise_id=seeded_history.id,
        sets=[
            {"effort_level": "failure"},      # RIR 0
            {"effort_level": "prefailure"},   # RIR 1
            {"effort_level": None},
            {"effort_level": None},
        ],
    )
    await db.commit()

    metric = await compute_effort_metric(db, test_user.id, PERIOD_START, PERIOD_END)

    assert metric.avg_rir == pytest.approx(0.5)
    assert metric.labeled_share == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_effort_metric_without_any_labels_reports_none(db, test_user, seeded_history):
    await _session_with_sets(
        db, test_user.id,
        started=datetime(2026, 8, 4, 10, tzinfo=timezone.utc),
        exercise_id=seeded_history.id, sets=[{"effort_level": None}] * 3,
    )
    await db.commit()

    metric = await compute_effort_metric(db, test_user.id, PERIOD_START, PERIOD_END)

    assert metric.avg_rir is None
    assert metric.labeled_share == 0.0


@pytest.mark.asyncio
async def test_relative_intensity_uses_baseline_before_the_period(db, test_user, seeded_history):
    """База e1RM — лучший результат ДО периода. Делить на сегодняшний e1RM
    нельзя: прошлые периоды занижались бы ровно на величину прогресса."""
    await _session_with_sets(
        db, test_user.id,
        started=datetime(2026, 7, 1, 10, tzinfo=timezone.utc),
        exercise_id=seeded_history.id,
        sets=[{"weight": 100.0, "reps": 1}],       # e1RM = 100
    )
    await _session_with_sets(
        db, test_user.id,
        started=datetime(2026, 8, 4, 10, tzinfo=timezone.utc),
        exercise_id=seeded_history.id,
        sets=[{"weight": 80.0, "reps": 3}],        # 80 % от базы
    )
    await db.commit()

    metric = await compute_intensity_metric(
        db, test_user.id, PERIOD_START, PERIOD_END, BASELINE_START
    )

    assert metric.avg_relative == pytest.approx(0.8)
    assert metric.heavy_set_share == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_intensity_is_none_when_no_baseline_exists(db, test_user, seeded_history):
    await _session_with_sets(
        db, test_user.id,
        started=datetime(2026, 8, 4, 10, tzinfo=timezone.utc),
        exercise_id=seeded_history.id, sets=[{"weight": 80.0, "reps": 3}],
    )
    await db.commit()

    metric = await compute_intensity_metric(
        db, test_user.id, PERIOD_START, PERIOD_END, BASELINE_START
    )

    assert metric.avg_relative is None


@pytest.mark.asyncio
async def test_baseline_window_agrees_with_period_window_on_finished_at(
    db, test_user, seeded_history
):
    """Обе половины отношения должны опираться на один и тот же критерий
    'сессия завершена'. status == 'finished' с null finished_at не в счёт
    ни там, ни там — иначе базовое окно училось бы на сессиях, которые
    _finished_sessions_in() выкинуло бы из окна периода."""
    dangling = WorkoutSession(
        app_user_id=test_user.id, source="free", status="finished",
        started_at=datetime(2026, 7, 1, 10, tzinfo=timezone.utc), finished_at=None,
    )
    db.add(dangling)
    await db.flush()
    se = WorkoutSessionExercise(
        workout_session_id=dangling.id, exercise_id=seeded_history.id, order_index=0,
    )
    db.add(se)
    await db.flush()
    db.add(WorkoutSessionSet(
        workout_session_exercise_id=se.id, set_number=1, set_type="normal",
        weight=100.0, reps=1, is_completed=True, is_anomalous=False,
    ))
    await db.flush()

    await _session_with_sets(
        db, test_user.id,
        started=datetime(2026, 8, 4, 10, tzinfo=timezone.utc),
        exercise_id=seeded_history.id,
        sets=[{"weight": 80.0, "reps": 3}],
    )
    await db.commit()

    metric = await compute_intensity_metric(
        db, test_user.id, PERIOD_START, PERIOD_END, BASELINE_START
    )

    assert metric.avg_relative is None


@pytest.mark.asyncio
async def test_records_are_limited_to_the_period(db, test_user, seeded_history):
    from api.services.models import UserRecord

    db.add(UserRecord(
        app_user_id=test_user.id, exercise_id=seeded_history.id,
        exercise_name=seeded_history.name, record_type="max_weight",
        value=100.0, date_achieved=date(2026, 8, 5),
    ))
    db.add(UserRecord(
        app_user_id=test_user.id, exercise_id=seeded_history.id,
        exercise_name=seeded_history.name, record_type="max_weight",
        value=95.0, date_achieved=date(2026, 7, 5),
    ))
    await db.commit()

    records = await collect_records(db, test_user.id, PERIOD_START, PERIOD_END)

    assert [r.value for r in records] == [100.0]


@pytest.mark.asyncio
async def test_has_activity_is_false_for_empty_period(db, test_user):
    metrics = await compute_metrics(db, test_user.id, "week", PERIOD_START, PERIOD_END, None)
    assert has_activity(metrics) is False
