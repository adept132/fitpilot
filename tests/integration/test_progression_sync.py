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
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import (
    AppUser,
    Exercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)


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
                # P0-06 C3: prescription теперь типизирован SyncPrescriptionSnapshot
                # (api/schemas/sync.py) — scheme и reason_code обязательны там же,
                # где их без .get() читает Prescription.from_dict. Тест проверяет
                # write-once (сервер должен ИГНОРИРОВАТЬ этот валидный-по-форме,
                # но семантически бессмысленный snapshot), а не отказ по форме —
                # поэтому reason_code присутствует, просто с абсурдным значением.
                "prescription": {
                    "scheme": "astrology",
                    "sets": [],
                    "reason_code": "astrology_reason",
                },
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


# --- P0-06 C3: типизация клиентского prescription на границе /sync/workouts ---
#
# Prescription.from_dict() (api/services/progression/types.py) читает
# scheme/reason_code/каждый sets[i].rep_min/rir БЕЗ .get() — до фикса сырой
# `dict` из SyncExerciseSnapshot.prescription летел в JSONB как есть, и
# первое же чтение истории (load_history) с такой строкой падало KeyError.
# Из-за write-once её было не перезаписать — упражнение становилось
# непригодным для завершения ЛЮБОЙ тренировки. Эти тесты проверяют, что
# граница синка отвергает такой payload 422-й, а не кладёт его в БД.


@pytest.mark.asyncio
async def test_sync_rejects_prescription_missing_required_fields(
    client, auth_headers, fresh_exercise
):
    """{'foo': 1} — воспроизведённый в ревью пример: KeyError('scheme')."""
    payload = {
        "client_uuid": "offline-workout-garbage-1",
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
                "client_uuid": "offline-ex-garbage-1",
                "exercise_id": fresh_exercise.id,
                "order_index": 0,
                "superset_group": None,
                "notes": None,
                "prescription": {"foo": 1},
                "sets": [],
            }
        ],
    }
    resp = await client.post("/sync/workouts", headers=auth_headers, json=payload)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_sync_rejects_prescription_with_incomplete_set(
    client, auth_headers, fresh_exercise
):
    """sets: [{'set_number': 1}] без rep_min/rir — воспроизведённый в ревью
    пример: KeyError('rep_min')."""
    payload = {
        "client_uuid": "offline-workout-garbage-2",
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
                "client_uuid": "offline-ex-garbage-2",
                "exercise_id": fresh_exercise.id,
                "order_index": 0,
                "superset_group": None,
                "notes": None,
                "prescription": {
                    "scheme": "double",
                    "reason_code": "progressed",
                    "sets": [{"set_number": 1}],
                },
                "sets": [],
            }
        ],
    }
    resp = await client.post("/sync/workouts", headers=auth_headers, json=payload)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_poisoned_stored_prescription_degrades_to_bootstrap_not_a_crash(
    client, auth_headers, db: AsyncSession, test_user: AppUser
):
    """Мусор, УЖЕ осевший в se.prescription (не через /sync/workouts — эту
    дверь C3 закрывает валидацией; здесь моделируется строка, попавшая в БД
    раньше или в обход API), не должен ронять чтение истории.

    load_history() (api/services/progression/repository.py) оборачивает
    Prescription.from_dict() в try/except и деградирует до prescription=None
    для этой сессии — движок для НОВОГО упражнения того же типа уходит в
    бутстрап (e1rm_factor), а не роняет запрос 500-й. Реальный вес в
    испорченной сессии (50кг x8) остаётся читаемым независимо от prescription
    (working_e1rm считается по SetFact, не по битому JSON), поэтому бутстрап
    отдаёт настоящую рекомендацию, а не голый no_basis.
    """
    marker = uuid.uuid4().hex[:8]
    ex = Exercise(
        name=f"Упражнение с битым prescription {marker}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(ex)
    await db.flush()

    workout = WorkoutSession(
        app_user_id=test_user.id,
        source="free",
        status="finished",
        finished_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    db.add(workout)
    await db.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id,
        exercise_id=ex.id,
        order_index=0,
        # Мусор в обход схемы (записан напрямую, не через /sync/workouts):
        # нет обязательных scheme/reason_code, которые Prescription.from_dict
        # читает без .get().
        prescription={"corrupted": True, "not_a_prescription_at_all": [1, 2, 3]},
    )
    db.add(se)
    await db.flush()

    db.add(
        WorkoutSessionSet(
            workout_session_exercise_id=se.id,
            set_number=1,
            set_type="normal",
            weight=50.0,
            reps=8,
            effort_level="medium",
            is_completed=True,
        )
    )
    await db.commit()

    workout_resp = (
        await client.post("/workouts/start", headers=auth_headers, json={"source": "free"})
    ).json()
    resp = await client.post(
        f"/workouts/{workout_resp['id']}/exercises",
        headers=auth_headers,
        json={"exercise_id": ex.id},
    )
    assert resp.status_code == 200, resp.text

    added = resp.json()["exercises"][-1]
    # Бутстрап (нет читаемой истории предписаний) -> e1rm_factor по факту
    # 50кг x8 в испорченной сессии, а не 500 и не голый no_basis.
    assert added["prescription"]["scheme"] == "e1rm_factor"
    assert added["recommended_weight"] is not None
