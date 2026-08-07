"""Регрессия на предикаты `performed_for` (спека §5.3): warmup-подходы и
подходы с `is_anomalous=True` не входят в фактический объём окна.

Раньше эти три сценария были закрыты тестами удалённого
`get_weekly_performed_sets` (tests/integration/test_statistics_service.py).
Функция ушла (P0-09, Задача 12) вместе с файлом, и находка финального ревью
Task 12 была в том, что вместе с ней ушла и защита от регрессии — ни один
оставшийся тест не проверяет, что `performed_for` фильтрует warmup и
аномальные подходы, а не просто попадает в них по пути. Этот модуль переносит
три потерянных утверждения на функцию, которая реально их считает
(api/services/volume/repository.py).
"""
from datetime import datetime, time, timedelta, timezone

import pytest

from api.services.models import (
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.volume.repository import Window, performed_for, utc_today

pytestmark = pytest.mark.asyncio


async def _seed_session_with_sets(db, user_id, exercise_id, started_on, specs):
    """Одна завершённая сессия с одним упражнением и подходами по `specs` —
    списку пар (set_type, is_anomalous), по одной на подход.

    Форма — по образцу `_seed_finished_session` в
    test_volume_window_snapshot.py (та же завершённая сессия с одним
    упражнением), но с управляемыми set_type/is_anomalous на каждый подход —
    ровно то, что нужно этим тестам и что делает variant в
    test_volume_window_snapshot.py негодным без изменения (там жёстко
    зашиты normal/False). Не импортируется оттуда намеренно (см. задание
    ревью): дублирование трёх строк дешевле, чем протаскивание приватного
    хелпера между модулями тестов.
    """
    session_row = WorkoutSession(
        app_user_id=user_id,
        source="free",
        status="finished",
        started_at=datetime.combine(started_on, time(12, 0), tzinfo=timezone.utc),
        finished_at=datetime.combine(started_on, time(13, 0), tzinfo=timezone.utc),
    )
    db.add(session_row)
    await db.flush()

    session_exercise = WorkoutSessionExercise(
        workout_session_id=session_row.id, exercise_id=exercise_id, order_index=0
    )
    db.add(session_exercise)
    await db.flush()

    for i, (set_type, is_anomalous) in enumerate(specs):
        db.add(WorkoutSessionSet(
            workout_session_exercise_id=session_exercise.id,
            set_number=i + 1,
            weight=50.0, reps=8, is_completed=True,
            set_type=set_type, is_anomalous=is_anomalous,
        ))
    await db.commit()
    return session_row


def _single_day_window(day):
    """Window, покрывающее ровно один день. block_id/phase_number/is_deload
    не читаются `performed_for` — только start_date/end_date и app_user_id
    из вызывающего кода, поэтому подставлены нейтральные значения."""
    return Window(
        block_id=None,
        window_index=1,
        phase_number=None,
        start_date=day,
        end_date=day,
        is_deload=False,
    )


async def test_warmup_sets_are_not_counted(db, test_user, seeded_history):
    """normal и drop считаются, warmup — нет: 2 normal + 1 drop + 3 warmup
    должны дать 3 прямых подхода, а не 6."""
    start = utc_today() - timedelta(days=1)
    specs = [
        ("normal", False), ("normal", False),
        ("drop", False),
        ("warmup", False), ("warmup", False), ("warmup", False),
    ]
    await _seed_session_with_sets(db, test_user.id, seeded_history.id, start, specs)

    performed = await performed_for(db, test_user.id, _single_day_window(start))

    muscle = seeded_history.main_muscle_group
    assert performed[muscle].direct == 3.0


async def test_anomalous_normal_sets_are_not_counted(db, test_user, seeded_history):
    """is_anomalous=True исключается даже у set_type="normal": 2 чистых +
    2 аномальных normal-подхода должны дать 2, а не 4."""
    start = utc_today() - timedelta(days=1)
    specs = [
        ("normal", False), ("normal", False),
        ("normal", True), ("normal", True),
    ]
    await _seed_session_with_sets(db, test_user.id, seeded_history.id, start, specs)

    performed = await performed_for(db, test_user.id, _single_day_window(start))

    muscle = seeded_history.main_muscle_group
    assert performed[muscle].direct == 2.0


async def test_empty_history_yields_empty_result(db, test_user):
    """Окно без единой сессии — пустой результат, а не падение или KeyError
    выше по цепочке."""
    start = utc_today() - timedelta(days=1)

    performed = await performed_for(db, test_user.id, _single_day_window(start))

    assert performed == {}
