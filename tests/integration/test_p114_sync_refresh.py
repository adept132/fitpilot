"""P1-14: боевой путь завершения тренировки идёт через POST /sync/workouts.

refresh_state вызывается только из workout_center.finish_workout, которую
приложение больше не дёргает (тренировка завершается в офлайн-репозитории
и уезжает синком). Без хука в этой ручке ни состояние прогрессии, ни
рекорды на живом пути не обновляются.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from api.services.models import UserExerciseProgressionState, WorkoutSession


def _exercise_snapshot(
    exercise_id: int, *, ex_client_uuid: str, order_index: int, weight: float, reps: int
) -> dict:
    return {
        "client_uuid": ex_client_uuid,
        "exercise_id": exercise_id,
        "order_index": order_index,
        "superset_group": None,
        "notes": None,
        "prescription": None,
        "sets": [
            {
                "client_uuid": f"{ex_client_uuid}-set-1",
                "set_number": 1,
                "set_type": "normal",
                "weight": weight,
                "reps": reps,
                "effort_level": "medium",
                "is_completed": True,
            }
        ],
    }


def _payload(
    exercise_id: int,
    *,
    client_uuid: str,
    weight: float,
    reps: int,
    status: str = "finished",
    extra_exercises: list[dict] | None = None,
) -> dict:
    return {
        "client_uuid": client_uuid,
        "source": "free",
        "status": status,
        "split_day_id": None,
        "plan_id": None,
        "notes": None,
        "volume_targets": None,
        "started_at": "2026-08-12T10:00:00Z",
        "finished_at": "2026-08-12T11:00:00Z" if status == "finished" else None,
        "exercises": [
            _exercise_snapshot(
                exercise_id,
                ex_client_uuid=f"{client_uuid}-ex-1",
                order_index=0,
                weight=weight,
                reps=reps,
            ),
            *(extra_exercises or []),
        ],
    }


async def _state_row(db, exercise_id: int):
    return (await db.execute(
        select(UserExerciseProgressionState).where(
            UserExerciseProgressionState.exercise_id == exercise_id,
        )
    )).scalar_one_or_none()


@pytest.mark.asyncio
async def test_sync_finished_workout_rebuilds_records(
    client, auth_headers, db, seeded_history,
):
    """Тренировка, завершённая офлайн, обновляет рекорды при приёме синка."""
    resp = await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_payload(seeded_history.id, client_uuid="p114-rec-1", weight=82.5, reps=5),
    )
    assert resp.status_code == 200, resp.text

    row = await _state_row(db, seeded_history.id)
    assert row is not None
    assert row.records["weight_at_reps"]["5"]["weight"] == 82.5


@pytest.mark.asyncio
async def test_sync_finished_workout_refreshes_next_prescription(
    client, auth_headers, db, seeded_history,
):
    """Без этого «в следующий раз: N кг» показывал бы вчерашнее число."""
    resp = await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_payload(seeded_history.id, client_uuid="p114-presc-1", weight=45.0, reps=10),
    )
    assert resp.status_code == 200, resp.text

    row = await _state_row(db, seeded_history.id)
    assert row is not None
    assert row.next_prescription is not None


async def _finished_workout_row(db, client_uuid: str):
    return (await db.execute(
        select(WorkoutSession).where(WorkoutSession.client_uuid == client_uuid)
    )).scalar_one_or_none()


@pytest.mark.asyncio
async def test_sync_survives_failing_recompute(
    client, auth_headers, db, seeded_history, monkeypatch,
):
    """Падение пересчёта не рвёт приём тренировки — её нельзя потерять.

    Патчим `rebuild_records` в пространстве имён `api.routers.sync`: ручка
    импортировала функцию себе по имени (`from ... import rebuild_records`),
    поэтому только патч атрибута НА МОДУЛЕ РУЧКИ реально перехватывает вызов
    — патч на исходном модуле (records_repository) ничего бы не поменял,
    так как sync.py уже держит свою собственную ссылку на объект функции.
    """
    import api.routers.sync as sync_module

    async def boom(*args, **kwargs):
        raise RuntimeError("пересчёт упал")

    monkeypatch.setattr(sync_module, "rebuild_records", boom)

    resp = await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_payload(seeded_history.id, client_uuid="p114-boom-1", weight=50.0, reps=8),
    )
    assert resp.status_code == 200, resp.text

    # Не только код ответа — сама тренировка обязана лечь в БД целиком.
    row = await _finished_workout_row(db, "p114-boom-1")
    assert row is not None
    assert row.status == "finished"


@pytest.mark.asyncio
async def test_sync_survives_failing_progression_recompute(
    client, auth_headers, db, seeded_history, monkeypatch,
):
    """Падение внутри самого движка прогрессии (build_context) — то же самое.

    В отличие от предыдущего теста, здесь падает не rebuild_records, а шаг
    ПОСЛЕ него — build_context на пути build_context -> plan_exercise ->
    refresh_state, который до фикса Finding 1 выполнялся вне guarded().
    Патчим `progression_repo.build_context` через сам модуль
    (`api.services.progression.repository`), а не через `sync_module`:
    sync.py вызывает `progression_repo.build_context(...)` как атрибут
    модуля на каждый вызов, а не через прямой импорт имени — патч на
    объекте модуля виден ручке, потому что это тот же объект в sys.modules.
    """
    from api.services.progression import repository as progression_repo

    async def boom(*args, **kwargs):
        raise RuntimeError("build_context упал")

    monkeypatch.setattr(progression_repo, "build_context", boom)

    resp = await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_payload(seeded_history.id, client_uuid="p114-boom-2", weight=55.0, reps=6),
    )
    assert resp.status_code == 200, resp.text

    row = await _finished_workout_row(db, "p114-boom-2")
    assert row is not None
    assert row.status == "finished"


@pytest.mark.asyncio
async def test_second_sync_recomputes_all_exercises_not_just_first(
    client, auth_headers, db, seeded_history, fresh_exercise,
):
    """Второй синк той же тренировки обязан пересчитать ВСЕ упражнения снимка.

    Регрессия ревью: реселект внутри `_load_inputs` без populate_existing=True
    возвращал объект тренировки из identity map с уже прогруженной (на строке
    `loaded_exercises = ... list(workout.exercises)` выше в этом же запросе)
    коллекцией exercises. Второе упражнение, добавленное этим же (вторым)
    синком, попадает в БД через сырой FK-инсерт, а не через relationship —
    в стухшей коллекции его нет, и `refreshed.exercises` его не отдаёт.
    Итог — рекорд/предписание для второго упражнения молча не пересчитывались.

    Первый POST — активная тренировка с одним упражнением (seeded_history).
    Второй POST по тому же client_uuid — та же тренировка, статус finished,
    то же упражнение плюс новое (fresh_exercise). Оба должны получить
    состояние прогрессии.
    """
    workout_client_uuid = "p114-refresh-2ex-1"

    active_payload = _payload(
        seeded_history.id,
        client_uuid=workout_client_uuid,
        weight=80.0,
        reps=5,
        status="active",
    )
    resp = await client.post("/sync/workouts", headers=auth_headers, json=active_payload)
    assert resp.status_code == 200, resp.text
    sync_version = resp.json()["sync_version"]

    second_exercise = _exercise_snapshot(
        fresh_exercise.id,
        ex_client_uuid=f"{workout_client_uuid}-ex-2",
        order_index=1,
        weight=40.0,
        reps=10,
    )
    finished_payload = _payload(
        seeded_history.id,
        client_uuid=workout_client_uuid,
        weight=80.0,
        reps=5,
        status="finished",
        extra_exercises=[second_exercise],
    )
    finished_payload["base_version"] = sync_version

    resp = await client.post("/sync/workouts", headers=auth_headers, json=finished_payload)
    assert resp.status_code == 200, resp.text

    first_state = await _state_row(db, seeded_history.id)
    second_state = await _state_row(db, fresh_exercise.id)

    assert first_state is not None
    assert second_state is not None


@pytest.mark.asyncio
async def test_changes_delta_carries_exercise_records(
    client, auth_headers, seeded_history,
):
    await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_payload(seeded_history.id, client_uuid="p114-delta-1", weight=82.5, reps=5),
    )

    resp = await client.get("/sync/changes", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    records = body["exercise_records"][str(seeded_history.id)]
    assert records["weight_at_reps"]["5"]["weight"] == 82.5
