"""P1-14: флаг is_max_reps — режим подхода, ортогональный set_type."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from api.services.models import WorkoutSessionSet


@pytest_asyncio.fixture
async def session_exercise_id(client, auth_headers, seeded_history):
    """Активная тренировка с одним упражнением, готовая принимать подходы."""
    workout = (await client.post(
        "/workouts/start", headers=auth_headers, json={"source": "free"},
    )).json()
    add = (await client.post(
        f"/workouts/{workout['id']}/exercises",
        headers=auth_headers,
        json={"exercise_id": seeded_history.id},
    )).json()
    return add["exercises"][-1]["id"]


@pytest.mark.asyncio
async def test_max_reps_flag_round_trips(client, auth_headers, db, session_exercise_id):
    resp = await client.post(
        f"/workout-session-exercises/{session_exercise_id}/sets",
        headers=auth_headers,
        json={"weight": 82.5, "reps": 9, "effort_level": "failure", "is_max_reps": True},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["is_max_reps"] is True

    row = (await db.execute(
        select(WorkoutSessionSet).where(WorkoutSessionSet.id == body["id"])
    )).scalar_one()
    assert row.is_max_reps is True


@pytest.mark.asyncio
async def test_max_reps_defaults_to_false(client, auth_headers, session_exercise_id):
    resp = await client.post(
        f"/workout-session-exercises/{session_exercise_id}/sets",
        headers=auth_headers,
        json={"weight": 82.5, "reps": 5, "effort_level": "medium"},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["is_max_reps"] is False


@pytest.mark.asyncio
async def test_max_reps_does_not_change_set_type(client, auth_headers, session_exercise_id):
    """Ортогональность осей: режим не подменяет роль подхода."""
    resp = await client.post(
        f"/workout-session-exercises/{session_exercise_id}/sets",
        headers=auth_headers,
        json={"weight": 82.5, "reps": 9, "effort_level": "failure", "is_max_reps": True},
    )
    assert resp.json()["set_type"] == "normal"


@pytest.mark.asyncio
async def test_max_reps_flag_survives_active_workout_read(client, auth_headers, session_exercise_id):
    """Находка ревью: флаг был виден только в эфемерном ответе создания подхода
    и пропадал при любой перезагрузке — GET /workouts/active, GET по id,
    подтверждении sync и пул-дельте. Реальный read-путь — единственная
    проверка, которая ловит регресс; прямой ORM-запрос её не заменяет."""
    create = await client.post(
        f"/workout-session-exercises/{session_exercise_id}/sets",
        headers=auth_headers,
        json={"weight": 82.5, "reps": 9, "effort_level": "failure", "is_max_reps": True},
    )
    assert create.status_code in (200, 201), create.text
    set_id = create.json()["id"]

    resp = await client.get("/workouts/active", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    found = None
    for exercise in body["exercises"]:
        for s in exercise["sets"]:
            if s["id"] == set_id:
                found = s
    assert found is not None, "созданный подход не найден в GET /workouts/active"
    assert found["is_max_reps"] is True


@pytest.mark.asyncio
async def test_repeat_set_inherits_max_reps_flag(client, auth_headers, session_exercise_id):
    """Повтор подхода-максимума должен остаться подходом-максимумом."""
    create = await client.post(
        f"/workout-session-exercises/{session_exercise_id}/sets",
        headers=auth_headers,
        json={"weight": 82.5, "reps": 9, "effort_level": "failure", "is_max_reps": True},
    )
    assert create.status_code in (200, 201), create.text
    set_id = create.json()["id"]

    repeat = await client.post(
        f"/workout-session-sets/{set_id}/repeat",
        headers=auth_headers,
        json={},
    )
    assert repeat.status_code in (200, 201), repeat.text
    assert repeat.json()["is_max_reps"] is True


@pytest.mark.asyncio
async def test_patch_without_is_max_reps_does_not_reset_flag(client, auth_headers, session_exercise_id):
    """exclude_unset=True в PATCH не должен затирать ранее выставленный флаг
    полем, которое клиент не прислал."""
    create = await client.post(
        f"/workout-session-exercises/{session_exercise_id}/sets",
        headers=auth_headers,
        json={"weight": 82.5, "reps": 9, "effort_level": "failure", "is_max_reps": True},
    )
    assert create.status_code in (200, 201), create.text
    set_id = create.json()["id"]

    patch = await client.patch(
        f"/workout-session-sets/{set_id}",
        headers=auth_headers,
        json={"reps": 10},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["is_max_reps"] is True


# --- P1-14, находка ревью 1: is_max_reps через боевой путь /sync/workouts ---
#
# Приложение больше не бьёт в POST /workout-session-exercises/{id}/sets — оно
# шлёт снимок целиком в /sync/workouts (см. test_p114_sync_refresh.py и
# docstring _apply_snapshot). Тесты выше это НЕ проверяют: они бьют в
# устаревшую ручку и поэтому не поймали бы, что _apply_snapshot раньше писал
# `workout_set.is_max_reps = set_snap.is_max_reps` безусловно — снимок,
# который поле не прислал (bool = False по умолчанию в SyncSetSnapshot),
# молча стирал уже выставленный флаг при каждом синке.


def _sync_set_snapshot(*, set_uuid: str, is_max_reps: bool | None) -> dict:
    payload = {
        "client_uuid": set_uuid,
        "set_number": 1,
        "set_type": "normal",
        "weight": 82.5,
        "reps": 9,
        "effort_level": "failure",
        "is_completed": True,
    }
    if is_max_reps is not None:
        payload["is_max_reps"] = is_max_reps
    return payload


def _sync_workout_snapshot(
    *,
    client_uuid: str,
    exercise_id: int,
    set_uuid: str,
    is_max_reps: bool | None,
    base_version: int | None = None,
) -> dict:
    return {
        "client_uuid": client_uuid,
        "base_version": base_version,
        "source": "free",
        "status": "active",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "exercises": [
            {
                "client_uuid": f"{client_uuid}-ex",
                "exercise_id": exercise_id,
                "order_index": 0,
                "sets": [
                    _sync_set_snapshot(set_uuid=set_uuid, is_max_reps=is_max_reps)
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_sync_snapshot_with_is_max_reps_true_persists_it(
    client, auth_headers, db, seeded_history,
):
    """Снимок, явно несущий is_max_reps: true, обязан его сохранить —
    это боевой путь, которым приложение реально отмечает max-reps подход."""
    client_uuid = f"p114-sync-max-{uuid.uuid4().hex[:8]}"
    set_uuid = f"{client_uuid}-set-1"

    resp = await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_sync_workout_snapshot(
            client_uuid=client_uuid,
            exercise_id=seeded_history.id,
            set_uuid=set_uuid,
            is_max_reps=True,
        ),
    )
    assert resp.status_code == 200, resp.text

    row = (await db.execute(
        select(WorkoutSessionSet).where(WorkoutSessionSet.client_uuid == set_uuid)
    )).scalar_one()
    assert row.is_max_reps is True


@pytest.mark.asyncio
async def test_sync_snapshot_omitting_is_max_reps_does_not_clear_existing_flag(
    client, auth_headers, db, seeded_history,
):
    """Находка ревью (P1-14, находка 1): мобильный снимок сейчас не шлёт
    is_max_reps вовсе (см. src/offline/snapshot.ts до фикса) — второй такой
    снимок по тому же подходу не должен стирать флаг, выставленный первым.
    Легаси-клиент, который поля не знает, не имеет права затирать чужую
    правду `false` поверх уже true."""
    client_uuid = f"p114-sync-omit-{uuid.uuid4().hex[:8]}"
    set_uuid = f"{client_uuid}-set-1"

    first = await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_sync_workout_snapshot(
            client_uuid=client_uuid,
            exercise_id=seeded_history.id,
            set_uuid=set_uuid,
            is_max_reps=True,
        ),
    )
    assert first.status_code == 200, first.text
    version = first.json()["sync_version"]

    row = (await db.execute(
        select(WorkoutSessionSet).where(WorkoutSessionSet.client_uuid == set_uuid)
    )).scalar_one()
    assert row.is_max_reps is True

    # Второй снимок того же подхода — БЕЗ ключа is_max_reps вообще (не
    # is_max_reps: false, а поле целиком отсутствует в теле запроса).
    second = await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_sync_workout_snapshot(
            client_uuid=client_uuid,
            exercise_id=seeded_history.id,
            set_uuid=set_uuid,
            is_max_reps=None,
            base_version=version,
        ),
    )
    assert second.status_code == 200, second.text

    await db.refresh(row)
    assert row.is_max_reps is True


# --- P1-14, находка ревью 2: is_anomalous в детальном read-схеме подхода ---
#
# WorkoutSessionSetResponse — та схема, что реально вложена в
# WorkoutSessionDetailResponse.exercises[].sets[] (GET /workouts/active, GET
# по id, ответ /sync/workouts, элементы /sync/changes.workouts). Вердикт
# аномальности до фикса был виден только в AddWorkoutSetResponse — ручке,
# которую приложение больше не вызывает на боевом пути (см. блок выше).


@pytest.mark.asyncio
async def test_sync_response_exposes_is_anomalous_on_set(
    client, auth_headers, db, seeded_history,
):
    """Абсурдный вес (>600 кг, без требования истории) — самый простой
    надёжный триггер is_anomalous, не зависящий от накопленной статистики."""
    client_uuid = f"p114-sync-anom-{uuid.uuid4().hex[:8]}"
    set_uuid = f"{client_uuid}-set-1"

    payload = _sync_workout_snapshot(
        client_uuid=client_uuid,
        exercise_id=seeded_history.id,
        set_uuid=set_uuid,
        is_max_reps=None,
    )
    payload["exercises"][0]["sets"][0]["weight"] = 700

    resp = await client.post("/sync/workouts", headers=auth_headers, json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    sets = body["workout"]["exercises"][0]["sets"]
    found = next(s for s in sets if s["client_uuid"] == set_uuid)
    assert found["is_anomalous"] is True

    # И на перечитывании тем же путём, каким его увидит клиент офлайн-пула.
    active = await client.get("/workouts/active", headers=auth_headers)
    assert active.status_code == 200, active.text
    active_sets = active.json()["exercises"][0]["sets"]
    active_found = next(s for s in active_sets if s["client_uuid"] == set_uuid)
    assert active_found["is_anomalous"] is True
