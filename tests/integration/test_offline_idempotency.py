"""Идемпотентность offline-синхронизации.

Клиент при реконнекте может отправить одну и ту же операцию повторно (например,
ответ сервера потерялся после коммита). Все три точки входа должны быть
идемпотентны по client_uuid и НЕ создавать дублей.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from api.services.models import (
    Exercise,
    UserAnthropometry,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)

pytestmark = pytest.mark.asyncio


async def _create_exercise(client) -> int:
    """Создаёт кастомное упражнение и возвращает его id (нужен валидный FK)."""
    resp = await client.post(
        "/exercises",
        json={
            "name": f"Тестовое {uuid.uuid4().hex[:8]}",
            "main_muscle_group": "Грудь",
            "client_uuid": uuid.uuid4().hex,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def test_custom_exercise_idempotent(client, db, test_user):
    """POST /exercises с одинаковым client_uuid не создаёт дубль."""
    client_uuid = uuid.uuid4().hex
    payload = {
        "name": f"Моё упражнение {uuid.uuid4().hex[:8]}",
        "main_muscle_group": "Спина",
        "client_uuid": client_uuid,
    }

    first = await client.post("/exercises", json=payload)
    assert first.status_code in (200, 201), first.text
    second = await client.post("/exercises", json=payload)
    assert second.status_code in (200, 201), second.text

    # Тот же id, а не 409/дубль.
    assert first.json()["id"] == second.json()["id"]

    count = await db.scalar(
        select(func.count())
        .select_from(Exercise)
        .where(
            Exercise.app_user_id == test_user.id,
            Exercise.client_uuid == client_uuid,
        )
    )
    assert count == 1


async def test_body_entry_idempotent(client, db, test_user):
    """POST /body/entry с одинаковым client_uuid не создаёт вторую запись."""
    client_uuid = uuid.uuid4().hex
    payload = {"weight": 80.5, "client_uuid": client_uuid}

    first = await client.post("/body/entry", json=payload)
    assert first.status_code == 200, first.text
    second = await client.post("/body/entry", json=payload)
    assert second.status_code == 200, second.text

    count = await db.scalar(
        select(func.count())
        .select_from(UserAnthropometry)
        .where(
            UserAnthropometry.app_user_id == test_user.id,
            UserAnthropometry.client_uuid == client_uuid,
        )
    )
    assert count == 1


async def test_sync_workout_idempotent(client, db, test_user):
    """Повторный POST /sync/workouts тем же снимком = одна тренировка без дублей."""
    exercise_id = await _create_exercise(client)

    w_uuid = uuid.uuid4().hex
    ex_uuid = uuid.uuid4().hex
    set1_uuid = uuid.uuid4().hex
    drop_uuid = uuid.uuid4().hex

    snapshot = {
        "client_uuid": w_uuid,
        "source": "free",
        "status": "finished",
        "started_at": "2026-07-22T10:00:00+00:00",
        "finished_at": "2026-07-22T11:00:00+00:00",
        "exercises": [
            {
                "client_uuid": ex_uuid,
                "exercise_id": exercise_id,
                "order_index": 0,
                "sets": [
                    {
                        "client_uuid": set1_uuid,
                        "set_number": 1,
                        "set_type": "normal",
                        "weight": 100,
                        "reps": 5,
                        "is_completed": True,
                    },
                    {
                        "client_uuid": drop_uuid,
                        "set_number": 2,
                        "set_type": "drop",
                        "weight": 80,
                        "reps": 6,
                        "is_completed": True,
                        "parent_client_uuid": set1_uuid,
                    },
                ],
            }
        ],
    }

    first = await client.post("/sync/workouts", json=snapshot)
    assert first.status_code == 200, first.text
    second = await client.post("/sync/workouts", json=snapshot)
    assert second.status_code == 200, second.text

    # Один и тот же серверный id тренировки.
    assert first.json()["id_map"][w_uuid] == second.json()["id_map"][w_uuid]

    workout_id = first.json()["id_map"][w_uuid]

    workouts = await db.scalar(
        select(func.count())
        .select_from(WorkoutSession)
        .where(
            WorkoutSession.app_user_id == test_user.id,
            WorkoutSession.client_uuid == w_uuid,
        )
    )
    assert workouts == 1

    exercises = await db.scalar(
        select(func.count())
        .select_from(WorkoutSessionExercise)
        .where(WorkoutSessionExercise.workout_session_id == workout_id)
    )
    assert exercises == 1

    ex_row_id = await db.scalar(
        select(WorkoutSessionExercise.id).where(
            WorkoutSessionExercise.workout_session_id == workout_id
        )
    )
    sets = await db.scalar(
        select(func.count())
        .select_from(WorkoutSessionSet)
        .where(WorkoutSessionSet.workout_session_exercise_id == ex_row_id)
    )
    assert sets == 2, "повторный синк не должен дублировать подходы"

    # Дропсет связан с родителем по client_uuid.
    parent_id = await db.scalar(
        select(WorkoutSessionSet.id).where(
            WorkoutSessionSet.client_uuid == set1_uuid
        )
    )
    drop_parent = await db.scalar(
        select(WorkoutSessionSet.parent_set_id).where(
            WorkoutSessionSet.client_uuid == drop_uuid
        )
    )
    assert drop_parent == parent_id


async def test_sync_workout_applies_updates(client, db, test_user):
    """Второй снимок с изменёнными данными обновляет, а не плодит строки (LWW)."""
    exercise_id = await _create_exercise(client)
    w_uuid = uuid.uuid4().hex
    ex_uuid = uuid.uuid4().hex
    set_uuid = uuid.uuid4().hex

    def snapshot(reps: int, status: str):
        return {
            "client_uuid": w_uuid,
            "source": "free",
            "status": status,
            "started_at": "2026-07-22T10:00:00+00:00",
            "finished_at": None if status == "active" else "2026-07-22T11:00:00+00:00",
            "exercises": [
                {
                    "client_uuid": ex_uuid,
                    "exercise_id": exercise_id,
                    "order_index": 0,
                    "sets": [
                        {
                            "client_uuid": set_uuid,
                            "set_number": 1,
                            "set_type": "normal",
                            "weight": 100,
                            "reps": reps,
                            "is_completed": True,
                        }
                    ],
                }
            ],
        }

    await client.post("/sync/workouts", json=snapshot(5, "active"))
    await client.post("/sync/workouts", json=snapshot(8, "finished"))

    reps = await db.scalar(
        select(WorkoutSessionSet.reps).where(
            WorkoutSessionSet.client_uuid == set_uuid
        )
    )
    assert reps == 8, "последняя запись должна выиграть (LWW)"

    status = await db.scalar(
        select(WorkoutSession.status).where(WorkoutSession.client_uuid == w_uuid)
    )
    assert status == "finished"
