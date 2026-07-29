"""Предписания в дельте синхронизации и защита write-once при merge (P0-06,
Задача 16).

Пути и форма ответа сверены по api/routers/sync.py и api/schemas/sync.py:
- GET /sync/changes отдаёт SyncChangesResponse.prescriptions
  (str(exercise_id) -> next_prescription) и вкладывает WorkoutSessionExercise
  .prescription в каждый эксземпляр WorkoutSessionExerciseResponse.
- POST /sync/workouts не затирает непустой exercise.prescription клиентским
  значением (write-once, api/routers/sync.py::_apply_snapshot).

Отступления от чернового брифа (проверено кодом, а не предположениями):

1. POST /sync/workouts принимает ОДИН SyncWorkoutSnapshot прямо в теле
   запроса (`async def sync_workout(payload: SyncWorkoutSnapshot, ...)`), а
   НЕ `{"workouts": [...]}`, как было в черновике. Обёртка списком даёт 422
   ("client_uuid"/"source"/... field required на верхнем уровне).

2. Фикстура `seeded_history` сама создаёт и коммитит завершённую тренировку
   (для истории, на которой движок строит предписание). Из-за этого у
   test_user в окне /sync/changes оказывается ДВЕ тренировки:
   тренировка-история из seeded_history (без prescription — создана прямой
   записью в БД, не через API) и тренировка finished_session_with_prescription
   (с prescription — создана через реальный HTTP-поток). Индексация
   `workouts[0]` из чернового брифа предполагала ровно одну тренировку и
   ловила случайную — тесты ниже находят нужную по id
   (`finished_session_with_prescription.workout_id`).

3. Четвёртый тест брифа использовал фикстуру `exercise` (db_session/app_user
   — некоммитящее соединение) вместе с HTTP-клиентом `client` (test_user) —
   ровно та комбинация двух соединений, что вызвала пятичасовую взаимную
   блокировку (см. комментарий в conftest.py). Заменено на `fresh_exercise`
   (db/test_user, коммитящее) — иначе клиент, ходящий через отдельное
   соединение на каждый HTTP-запрос, попросту не увидел бы некоммитящую
   exercise-строку.
"""

from __future__ import annotations

import uuid

import pytest


def _find_workout(body: dict, workout_id: int) -> dict:
    """Найти в дельте тренировку по серверному id — а не по индексу: у
    test_user в окне /sync/changes может быть больше одной тренировки
    (см. докстринг модуля, пункт 2)."""
    for w in body["workouts"]:
        if w["id"] == workout_id:
            return w
    raise AssertionError(f"workout {workout_id} not found in /sync/changes delta")


@pytest.mark.asyncio
async def test_changes_delta_carries_next_prescriptions(
    client, auth_headers, finished_session_with_prescription
):
    """Завершение сессии кладёт next_prescription в кэш состояния — дельта
    синхронизации обязана отдать его целиком, по ключу str(exercise_id)."""
    resp = await client.get("/sync/changes", headers=auth_headers)
    assert resp.status_code == 200

    body = resp.json()
    assert "prescriptions" in body
    key = str(finished_session_with_prescription.exercise_id)
    assert key in body["prescriptions"]
    assert body["prescriptions"][key]["reason_code"]
    assert body["prescriptions"][key]["provisional"] is True


@pytest.mark.asyncio
async def test_workout_in_delta_carries_its_prescription(
    client, auth_headers, finished_session_with_prescription
):
    """Помимо предварительных предписаний, сама тренировка в дельте несёт
    предписание, выданное упражнению при добавлении в сессию."""
    body = (await client.get("/sync/changes", headers=auth_headers)).json()
    workout = _find_workout(body, finished_session_with_prescription.workout_id)
    exercises = workout["exercises"]
    assert exercises[0]["prescription"] is not None
    assert exercises[0]["prescription"]["reason_code"]


@pytest.mark.asyncio
async def test_sync_does_not_overwrite_an_existing_prescription(
    client, auth_headers, finished_session_with_prescription
):
    """Пользователь тренировался офлайн против того, что видел. Не подменяем.

    Тренировка и упражнение уже существуют на сервере (созданы через
    /workouts/start и /workouts/{id}/exercises) без client_uuid — снимок
    матчится к ним через server_id (адаптация, см. комментарий в
    api/routers/sync.py::_apply_snapshot: "Адаптация: тренировка создана
    ранее без client_uuid"), как это реально делает клиент при первой
    синхронизации существующих данных.
    """
    before = (await client.get("/sync/changes", headers=auth_headers)).json()
    snapshot = _find_workout(before, finished_session_with_prescription.workout_id)
    original_scheme = snapshot["exercises"][0]["prescription"]["scheme"]

    payload = {
        "client_uuid": str(uuid.uuid4()),
        "server_id": snapshot["id"],
        "base_version": before["versions"][str(snapshot["id"])],
        "source": snapshot["source"],
        "status": snapshot["status"],
        "split_day_id": None,
        "plan_id": None,
        "notes": None,
        "volume_targets": None,
        "started_at": snapshot["started_at"],
        "finished_at": snapshot["finished_at"],
        "exercises": [
            {
                "client_uuid": str(uuid.uuid4()),
                "server_id": e["id"],
                "exercise_id": e["exercise"]["id"],
                "order_index": e["order_index"],
                "superset_group": None,
                "notes": None,
                "prescription": {"scheme": "astrology", "sets": []},
                "sets": [],
            }
            for e in snapshot["exercises"]
        ],
    }
    resp = await client.post("/sync/workouts", headers=auth_headers, json=payload)
    assert resp.status_code == 200, resp.text

    after = (await client.get("/sync/changes", headers=auth_headers)).json()
    after_workout = _find_workout(after, finished_session_with_prescription.workout_id)
    saved = after_workout["exercises"][0]["prescription"]
    assert saved is not None
    assert saved["scheme"] != "astrology"
    assert saved["scheme"] == original_scheme


@pytest.mark.asyncio
async def test_client_prescription_is_accepted_when_server_has_none(
    client, auth_headers, fresh_exercise
):
    """Упражнение, добавленное офлайн: предписание пришло с устройства.

    fresh_exercise (db/test_user, коммитящее соединение) — а не `exercise`
    из брифа (db_session/app_user, некоммитящее): последнюю HTTP-клиент
    `client` попросту не увидел бы, а смешение соединений в одном тесте — та
    самая вечная блокировка (см. докстринг модуля, пункт 3).
    """
    payload = {
        "client_uuid": "offline-workout-1",
        "source": "free",
        "status": "finished",
        "split_day_id": None,
        "plan_id": None,
        "notes": None,
        "volume_targets": None,
        "started_at": "2026-07-27T10:00:00Z",
        "finished_at": "2026-07-27T11:00:00Z",
        "exercises": [
            {
                "client_uuid": "offline-ex-1",
                "exercise_id": fresh_exercise.id,
                "order_index": 0,
                "superset_group": None,
                "notes": None,
                "prescription": {
                    "scheme": "double",
                    "sets": [
                        {
                            "set_number": 1,
                            "weight_kg": 40.0,
                            "rep_min": 8,
                            "rep_max": 12,
                            "rir": 2,
                            "kind": "normal",
                        }
                    ],
                    "reason_code": "progressed",
                    "reason_text": "x",
                    "basis": {},
                    "engine_version": 1,
                    "provisional": True,
                },
                "sets": [],
            }
        ],
    }
    resp = await client.post("/sync/workouts", headers=auth_headers, json=payload)
    assert resp.status_code == 200, resp.text
    workout_id = resp.json()["id_map"]["offline-workout-1"]

    after = (await client.get("/sync/changes", headers=auth_headers)).json()
    after_workout = _find_workout(after, workout_id)
    saved = after_workout["exercises"][0]["prescription"]
    assert saved["scheme"] == "double"
