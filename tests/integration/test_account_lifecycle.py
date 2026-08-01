"""Жизненный цикл аккаунта (P0-04): удаление данных, grace period, доступ.

Главный тест здесь — что удаление ДЕЙСТВИТЕЛЬНО сносит все данные. Наивный
`DELETE FROM app_users` этого не делает: у workout_session_exercises в живой БД
нет FK на workout_sessions, поэтому строки осиротели бы, а RESTRICT на
exercises заблокировал бы удаление кастомных упражнений целиком.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from api.services.account_service import (
    DELETION_GRACE_PERIOD,
    purge_expired,
    purge_user,
)
from api.services.models import (
    AppUser,
    Exercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)

pytestmark = pytest.mark.asyncio


async def _seed_workout(client) -> tuple[int, str]:
    """Создаёт кастомное упражнение и тренировку с подходом. Возвращает
    (exercise_id, client_uuid тренировки)."""
    ex = await client.post(
        "/exercises",
        json={
            "name": f"Личное {uuid.uuid4().hex[:8]}",
            "main_muscle_group": "Грудь",
            "client_uuid": uuid.uuid4().hex,
        },
    )
    assert ex.status_code in (200, 201), ex.text
    exercise_id = ex.json()["id"]

    client_uuid = uuid.uuid4().hex
    resp = await client.post(
        "/sync/workouts",
        json={
            "client_uuid": client_uuid,
            "source": "free",
            "status": "finished",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "exercises": [
                {
                    "client_uuid": f"ex-{client_uuid}",
                    "exercise_id": exercise_id,
                    "order_index": 0,
                    "sets": [
                        {
                            "client_uuid": f"set-{client_uuid}",
                            "set_number": 1,
                            "weight": 100,
                            "reps": 5,
                        }
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    return exercise_id, client_uuid


async def test_purge_removes_every_trace(client, db, test_user):
    """После чистки не остаётся ни пользователя, ни тренировок, ни ДОЧЕРНИХ
    строк, ни кастомных упражнений."""
    exercise_id, _ = await _seed_workout(client)
    user_id = test_user.id

    # Убеждаемся, что данные действительно есть.
    sessions = (
        await db.execute(
            select(WorkoutSession.id).where(WorkoutSession.app_user_id == user_id)
        )
    ).scalars().all()
    assert sessions
    se_ids = (
        await db.execute(
            select(WorkoutSessionExercise.id).where(
                WorkoutSessionExercise.workout_session_id.in_(sessions)
            )
        )
    ).scalars().all()
    assert se_ids

    counts = await purge_user(db, user_id)
    assert counts["workouts"] >= 1
    assert counts["session_exercises"] >= 1
    assert counts["sets"] >= 1

    # Пользователя нет.
    assert await db.scalar(
        select(func.count()).select_from(AppUser).where(AppUser.id == user_id)
    ) == 0
    # Тренировок нет.
    assert await db.scalar(
        select(func.count())
        .select_from(WorkoutSession)
        .where(WorkoutSession.app_user_id == user_id)
    ) == 0
    # Дочерних строк нет — именно их наивный каскад оставлял сиротами.
    assert await db.scalar(
        select(func.count())
        .select_from(WorkoutSessionExercise)
        .where(WorkoutSessionExercise.id.in_(se_ids))
    ) == 0
    assert await db.scalar(
        select(func.count())
        .select_from(WorkoutSessionSet)
        .where(WorkoutSessionSet.workout_session_exercise_id.in_(se_ids))
    ) == 0
    # Кастомное упражнение унёс каскад — RESTRICT его больше не держит.
    assert await db.scalar(
        select(func.count()).select_from(Exercise).where(Exercise.id == exercise_id)
    ) == 0


async def test_naive_cascade_would_have_failed(client, db, test_user):
    """Документирует, почему purge_user написан вручную.

    Прямой DELETE FROM app_users падает: осиротевшие workout_session_exercises
    держат кастомное упражнение через ON DELETE RESTRICT.
    """
    await _seed_workout(client)
    user_id = test_user.id

    # Имитируем ровно то, что делает каскад: сносим только тренировки.
    await db.execute(
        text("DELETE FROM workout_sessions WHERE app_user_id = :uid"),
        {"uid": user_id},
    )
    await db.commit()

    orphans = await db.scalar(
        text(
            "SELECT count(*) FROM workout_session_exercises e "
            "WHERE NOT EXISTS (SELECT 1 FROM workout_sessions s WHERE s.id = e.workout_session_id) "
            "AND e.exercise_id IN (SELECT id FROM exercises WHERE app_user_id = :uid)"
        ),
        {"uid": user_id},
    )
    # Сироты действительно остаются — FK на workout_sessions в БД нет.
    assert orphans >= 1

    # И именно они ломают удаление пользователя «в лоб».
    with pytest.raises(Exception):
        await db.execute(
            text("DELETE FROM app_users WHERE id = :uid"), {"uid": user_id}
        )
        await db.commit()
    await db.rollback()

    # А наш purge с этим справляется.
    await purge_user(db, user_id)
    assert await db.scalar(
        select(func.count()).select_from(AppUser).where(AppUser.id == user_id)
    ) == 0


async def test_deletion_request_blocks_api_but_keeps_data(client, db, test_user):
    """Заявка закрывает доступ, но данные остаются — на случай «передумал»."""
    await _seed_workout(client)

    requested = await client.post("/account/deletion")
    assert requested.status_code == 200, requested.text
    body = requested.json()
    assert body["deletion_requested_at"]
    assert body["grace_period_days"] == DELETION_GRACE_PERIOD.days

    # Обычные эндпоинты закрыты.
    blocked = await client.get("/workouts/active")
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["detail"]["error"] == "account_pending_deletion"

    # Но данные на месте.
    assert await db.scalar(
        select(func.count())
        .select_from(WorkoutSession)
        .where(WorkoutSession.app_user_id == test_user.id)
    ) >= 1

    # И статус аккаунта смотреть можно — иначе не увидеть срок.
    status_resp = await client.get("/account")
    assert status_resp.status_code == 200
    assert status_resp.json()["purge_at"]


async def test_cancel_deletion_restores_access(client):
    await client.post("/account/deletion")
    assert (await client.get("/workouts/active")).status_code == 403

    cancelled = await client.delete("/account/deletion")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["deletion_requested_at"] is None

    # Доступ вернулся (404/200 — что угодно, кроме 403).
    assert (await client.get("/workouts/active")).status_code != 403


async def test_repeat_request_does_not_extend_grace(client):
    first = (await client.post("/account/deletion")).json()
    second = (await client.post("/account/deletion")).json()
    # Случайный двойной тап не должен отодвигать дату удаления.
    assert first["deletion_requested_at"] == second["deletion_requested_at"]
    assert first["purge_at"] == second["purge_at"]


async def test_purge_expired_respects_grace_period(client, db, test_user):
    await _seed_workout(client)
    await client.post("/account/deletion")

    # Срок ещё не истёк — не трогаем.
    purged = await purge_expired(db, now=datetime.now(timezone.utc))
    assert test_user.id not in purged
    assert await db.scalar(
        select(func.count()).select_from(AppUser).where(AppUser.id == test_user.id)
    ) == 1

    # Срок истёк — удаляем.
    later = datetime.now(timezone.utc) + DELETION_GRACE_PERIOD + timedelta(days=1)
    purged = await purge_expired(db, now=later)
    assert test_user.id in purged
    assert await db.scalar(
        select(func.count()).select_from(AppUser).where(AppUser.id == test_user.id)
    ) == 0


async def test_data_summary_counts_what_will_be_deleted(client):
    await _seed_workout(client)

    resp = await client.get("/account/data-summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workouts"] >= 1
    assert body["sets"] >= 1
    assert body["custom_exercises"] >= 1
