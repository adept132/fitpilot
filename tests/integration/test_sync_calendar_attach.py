"""P0-09 C1 (Critical): реальный путь завершения тренировки — POST /sync/workouts,
а не устаревший /workouts/{id}/finish, которым клиент не пользуется.

`attach_session_to_day` до фикса вызывался только с мёртвого эндпоинта, и
календарь никогда не узнавал о фактически выполненных тренировках, пришедших
через офлайн-очередь синка. `mark_missed_days` тем временем исправно помечал
те же дни пропущенными на следующий обход контекста — то есть каждый реально
выполненный день превращался в missed. Тесты здесь бьют РЕАЛЬНУЮ ручку синка
и падали бы без правки в api/routers/sync.py (_apply_snapshot).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.services.models import UserCalendarDay
from api.services.volume.repository import mark_missed_days, utc_today

pytestmark = pytest.mark.asyncio


async def _exercise_id(client) -> int:
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


def _finished_snapshot(client_uuid: str, exercise_id: int, started_at: datetime):
    return {
        "client_uuid": client_uuid,
        "base_version": None,
        "source": "free",
        "status": "finished",
        "started_at": started_at.isoformat(),
        "finished_at": (started_at + timedelta(minutes=40)).isoformat(),
        "deleted": False,
        "exercises": [
            {
                "client_uuid": f"ex-{client_uuid}",
                "exercise_id": exercise_id,
                "order_index": 0,
                "sets": [
                    {
                        "client_uuid": f"set-{client_uuid}",
                        "set_number": 1,
                        "weight": 60,
                        "reps": 8,
                        "is_completed": True,
                    }
                ],
            }
        ],
    }


async def test_sync_workouts_finish_attaches_calendar_day(client, db, test_user):
    """POST /sync/workouts с status=finished закрывает день календаря.

    Это единственный путь, которым реально завершают тренировку офлайн-первые
    клиенты (см. докстринг offline-репозитория). Без хука в _apply_snapshot
    день остаётся planned навсегда, несмотря на выполненную сессию.
    """
    today = utc_today()
    db.add(UserCalendarDay(
        app_user_id=test_user.id,
        target_date=today,
        is_rest_day=False,
        is_blackout=False,
        status="planned",
    ))
    await db.commit()

    exercise_id = await _exercise_id(client)
    client_uuid = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc)

    resp = await client.post(
        "/sync/workouts",
        json=_finished_snapshot(client_uuid, exercise_id, started_at),
    )
    assert resp.status_code == 200, resp.text
    workout_id = resp.json()["workout"]["id"]

    day = (await db.execute(
        select(UserCalendarDay).where(
            UserCalendarDay.app_user_id == test_user.id,
            UserCalendarDay.target_date == today,
        )
    )).scalar_one()

    assert day.status == "completed"
    assert day.actual_workout_session_id == workout_id


async def test_synced_finished_day_survives_mark_missed_days(client, db, test_user):
    """Регрессия на «adherence структурно ноль навсегда»: день, чей факт
    доехал через /sync/workouts, не должен превращаться в missed на
    следующем ленивом обходе контекста, даже если дата уже в прошлом.
    """
    yesterday = utc_today() - timedelta(days=1)
    db.add(UserCalendarDay(
        app_user_id=test_user.id,
        target_date=yesterday,
        is_rest_day=False,
        is_blackout=False,
        status="planned",
    ))
    await db.commit()

    exercise_id = await _exercise_id(client)
    client_uuid = uuid.uuid4().hex
    started_at = datetime.combine(yesterday, datetime.min.time()).replace(
        hour=10, tzinfo=timezone.utc
    )

    resp = await client.post(
        "/sync/workouts",
        json=_finished_snapshot(client_uuid, exercise_id, started_at),
    )
    assert resp.status_code == 200, resp.text

    # Тот же ленивый проход, что запускает build_context на каждый заход в
    # workout-center: если хук в sync не сработал, день сейчас всё ещё
    # planned и провалится ровно в missed.
    await mark_missed_days(db, test_user.id, utc_today())
    await db.commit()

    day = (await db.execute(
        select(UserCalendarDay).where(
            UserCalendarDay.app_user_id == test_user.id,
            UserCalendarDay.target_date == yesterday,
        )
    )).scalar_one()

    assert day.status == "completed"
