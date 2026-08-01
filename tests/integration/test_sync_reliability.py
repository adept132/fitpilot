"""Надёжность облачной синхронизации (P0-03).

Каждый тест — регрессия на конкретный дефект, найденный при аудите:
конкурентные пуши создавали дубль, устаревший снимок молча затирал чужие
правки, удаление не доезжало до второго устройства, дельты не было вовсе.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from api.services.models import (
    SyncTombstone,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)

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


def _snapshot(
    client_uuid: str,
    exercise_id: int,
    *,
    base_version=None,
    weight=100,
    set_uuid: str | None = None,
    ex_uuid: str | None = None,
    deleted=False,
):
    return {
        "client_uuid": client_uuid,
        "base_version": base_version,
        "source": "free",
        "status": "active",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "deleted": deleted,
        "exercises": [
            {
                "client_uuid": ex_uuid or f"ex-{client_uuid}",
                "exercise_id": exercise_id,
                "order_index": 0,
                "sets": [
                    {
                        "client_uuid": set_uuid or f"set-{client_uuid}",
                        "set_number": 1,
                        "weight": weight,
                        "reps": 5,
                    }
                ],
            }
        ],
    }


async def test_concurrent_push_creates_single_workout(client, db, test_user):
    """Два одновременных пуша одного снимка → одна строка, а не дубль.

    Раньше роутер делал read-then-insert без блокировки и без уникального
    индекса: оба запроса не находили строку и оба её вставляли.
    """
    exercise_id = await _exercise_id(client)
    client_uuid = uuid.uuid4().hex
    snap = _snapshot(client_uuid, exercise_id)

    first, second = await asyncio.gather(
        client.post("/sync/workouts", json=snap),
        client.post("/sync/workouts", json=snap),
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    count = await db.scalar(
        select(func.count())
        .select_from(WorkoutSession)
        .where(
            WorkoutSession.app_user_id == test_user.id,
            WorkoutSession.client_uuid == client_uuid,
        )
    )
    assert count == 1

    # Версии строго последовательны — значит второй запрос увидел коммит первого,
    # а не отработал параллельно на той же исходной версии.
    versions = sorted([first.json()["sync_version"], second.json()["sync_version"]])
    assert versions == [1, 2]


async def test_stale_snapshot_rejected_with_conflict(client, test_user):
    """Снимок на устаревшей версии получает 409 с актуальным состоянием."""
    exercise_id = await _exercise_id(client)
    client_uuid = uuid.uuid4().hex

    created = await client.post(
        "/sync/workouts", json=_snapshot(client_uuid, exercise_id)
    )
    assert created.status_code == 200, created.text
    current = created.json()["sync_version"]

    stale = await client.post(
        "/sync/workouts",
        json=_snapshot(client_uuid, exercise_id, base_version=current - 1, weight=999),
    )
    assert stale.status_code == 409, stale.text

    detail = stale.json()["detail"]
    assert detail["sync_version"] == current
    # Тело конфликта обязано нести состояние для merge, иначе клиенту нечего сливать.
    assert detail["workout"]["client_uuid"] == client_uuid
    assert len(detail["workout"]["exercises"]) == 1


async def test_fresh_snapshot_accepted_and_bumps_version(client, test_user):
    exercise_id = await _exercise_id(client)
    client_uuid = uuid.uuid4().hex

    first = await client.post(
        "/sync/workouts", json=_snapshot(client_uuid, exercise_id)
    )
    version = first.json()["sync_version"]

    second = await client.post(
        "/sync/workouts",
        json=_snapshot(client_uuid, exercise_id, base_version=version, weight=120),
    )
    assert second.status_code == 200, second.text
    assert second.json()["sync_version"] == version + 1
    # Повтор не размножил подходы.
    assert len(second.json()["workout"]["exercises"][0]["sets"]) == 1


async def test_detail_exposes_client_uuid(client):
    """Без client_uuid в ответе клиент не может сопоставить серверную строку
    со своей локальной — ни merge, ни защита от дублей при pull не работают."""
    exercise_id = await _exercise_id(client)
    client_uuid = uuid.uuid4().hex
    ex_uuid = f"ex-{client_uuid}"
    set_uuid = f"set-{client_uuid}"

    resp = await client.post(
        "/sync/workouts",
        json=_snapshot(
            client_uuid, exercise_id, ex_uuid=ex_uuid, set_uuid=set_uuid
        ),
    )
    workout = resp.json()["workout"]

    assert workout["client_uuid"] == client_uuid
    assert workout["exercises"][0]["client_uuid"] == ex_uuid
    assert workout["exercises"][0]["sets"][0]["client_uuid"] == set_uuid


async def test_changes_returns_delta_and_versions(client):
    since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    exercise_id = await _exercise_id(client)
    client_uuid = uuid.uuid4().hex

    created = await client.post(
        "/sync/workouts", json=_snapshot(client_uuid, exercise_id)
    )
    workout_id = created.json()["workout"]["id"]

    resp = await client.get("/sync/changes", params={"since": since})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    found = [w for w in body["workouts"] if w["client_uuid"] == client_uuid]
    assert len(found) == 1
    assert body["versions"][str(workout_id)] == created.json()["sync_version"]
    assert body["server_time"]


async def test_initial_pull_returns_active_session(client):
    """Первый pull (без курсора) обязан вернуть незавершённую сессию —
    это и есть «восстановление на новом устройстве»."""
    exercise_id = await _exercise_id(client)
    client_uuid = uuid.uuid4().hex
    await client.post("/sync/workouts", json=_snapshot(client_uuid, exercise_id))

    resp = await client.get("/sync/changes")
    assert resp.status_code == 200, resp.text
    assert any(w["client_uuid"] == client_uuid for w in resp.json()["workouts"])


async def test_delete_leaves_tombstone_for_other_devices(client, db, test_user):
    """Хард-delete не оставляет следа, поэтому удаление обязано писать надгробие —
    иначе второе устройство никогда не узнает, что тренировки больше нет."""
    since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    exercise_id = await _exercise_id(client)
    client_uuid = uuid.uuid4().hex

    await client.post("/sync/workouts", json=_snapshot(client_uuid, exercise_id))
    removed = await client.post(
        "/sync/workouts", json=_snapshot(client_uuid, exercise_id, deleted=True)
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted"] is True

    count = await db.scalar(
        select(func.count())
        .select_from(WorkoutSession)
        .where(WorkoutSession.client_uuid == client_uuid)
    )
    assert count == 0

    resp = await client.get("/sync/changes", params={"since": since})
    assert any(
        t["client_uuid"] == client_uuid for t in resp.json()["tombstones"]
    )

    # Уборка: надгробия не привязаны к каскаду удаления тренировок.
    async with db.begin_nested():
        rows = (
            await db.execute(
                select(SyncTombstone).where(
                    SyncTombstone.client_uuid == client_uuid
                )
            )
        ).scalars().all()
        for row in rows:
            await db.delete(row)
    await db.commit()


async def test_child_tombstone_removes_set(client, db):
    """Подход с deleted=true исчезает и на сервере (иначе он «воскресал» бы
    на других устройствах после каждого pull)."""
    exercise_id = await _exercise_id(client)
    client_uuid = uuid.uuid4().hex
    ex_uuid = f"ex-{client_uuid}"
    set_uuid = f"set-{client_uuid}"

    first = await client.post(
        "/sync/workouts",
        json=_snapshot(client_uuid, exercise_id, ex_uuid=ex_uuid, set_uuid=set_uuid),
    )
    version = first.json()["sync_version"]

    snap = _snapshot(client_uuid, exercise_id, base_version=version, ex_uuid=ex_uuid)
    snap["exercises"][0]["sets"] = [
        {"client_uuid": set_uuid, "set_number": 1, "deleted": True}
    ]
    resp = await client.post("/sync/workouts", json=snap)
    assert resp.status_code == 200, resp.text
    assert resp.json()["workout"]["exercises"][0]["sets"] == []
