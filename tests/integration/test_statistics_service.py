"""Интеграционный тест недельного среза подходов (statistics_service).

Пиннит поведение фильтра get_weekly_performed_sets: считаются только рабочие
подходы (normal, drop) и дропсеты, разминочные (warmup) — исключаются.
Идёт против реальной Postgres (см. tests/integration/conftest.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from api.services.models import (
    Exercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.statistics_service import get_weekly_performed_sets

pytestmark = pytest.mark.asyncio


async def _make_session_with_sets(db, user_id, exercise_id, set_types, anomalous_flags=None):
    """Создаёт завершённую сессию с подходами перечисленных типов.

    anomalous_flags — необязательный список bool той же длины, что и set_types,
    задающий is_anomalous для каждого подхода (по умолчанию все False).
    """
    session = WorkoutSession(
        app_user_id=user_id,
        source="free",
        status="finished",
        started_at=datetime.now(timezone.utc),
    )
    db.add(session)
    await db.flush()

    se = WorkoutSessionExercise(
        workout_session_id=session.id, exercise_id=exercise_id, order_index=0
    )
    db.add(se)
    await db.flush()

    if anomalous_flags is None:
        anomalous_flags = [False] * len(set_types)

    for i, (set_type, is_anomalous) in enumerate(zip(set_types, anomalous_flags), start=1):
        db.add(
            WorkoutSessionSet(
                workout_session_exercise_id=se.id,
                set_number=i,
                set_type=set_type,
                weight=100,
                reps=10,
                is_completed=True,
                is_anomalous=is_anomalous,
            )
        )
    await db.flush()
    return session


async def test_counts_only_working_sets(db, test_user):
    """normal + drop считаются, warmup — нет: результат должен быть 3, а не 4."""
    exercise = (
        await db.execute(select(Exercise).limit(1))
    ).scalar_one_or_none()
    assert exercise is not None, "в справочнике должно быть хотя бы одно упражнение"

    await _make_session_with_sets(
        db, test_user.id, exercise.id,
        ["normal", "normal", "drop", "warmup"],
    )
    await db.commit()

    result = await get_weekly_performed_sets(db, test_user.id)

    # normal + normal + drop = 3; разминка не считается.
    assert result[exercise.main_muscle_group] == 3


async def test_excludes_anomalous_sets(db, test_user):
    """Аномальный подход (is_anomalous=True) не должен учитываться в бюджете
    объёма (спека §5.3): 2 нормальных + 1 аномальный normal-подход -> 2, а не 3."""
    exercise = (
        await db.execute(select(Exercise).limit(1))
    ).scalar_one_or_none()
    assert exercise is not None, "в справочнике должно быть хотя бы одно упражнение"

    await _make_session_with_sets(
        db, test_user.id, exercise.id,
        ["normal", "normal", "normal"],
        anomalous_flags=[False, False, True],
    )
    await db.commit()

    result = await get_weekly_performed_sets(db, test_user.id)

    # 2 нормальных подхода считаются, аномальный (несмотря на set_type="normal") - нет.
    assert result[exercise.main_muscle_group] == 2


async def test_empty_history_returns_empty_dict(db, test_user):
    """У свежего пользователя без тренировок функция возвращает пустой словарь."""
    result = await get_weekly_performed_sets(db, test_user.id)
    assert result == {}
