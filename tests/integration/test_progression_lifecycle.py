"""Движок в жизненном цикле тренировки: добавление упражнения и завершение.

Проверяет три точки подключения движка (P0-06, Задача 14):
1. добавление упражнения в сессию сразу считает и сохраняет предписание;
2. завершение сессии обновляет кэш состояния и кладёт предварительное
   предписание на следующий раз;
3. публичный эндпоинт автопрогрессии не переписывает уже сохранённое
   (write-once).

Пути эндпоинтов сверены по api/routers/workouts.py и
api/routers/workout_center.py: создание тренировки — POST /workouts/start
(а не POST /workouts, как в черновике брифа), завершение — POST
/workouts/{id}/finish живёт в workout_center.py.

Фикстуры seeded_history/fresh_exercise используют `db`/`test_user`
(коммитящее соединение), а НЕ `db_session`/`app_user` — здесь тесты ходят
через HTTP-клиент `client`, который сам завязан на test_user и открывает
собственные соединения на каждый запрос; смешивание с db_session/app_user
в одном тесте воспроизводит вечную блокировку, из-за которой предыдущая
задача не завершилась за пять часов (см. комментарий в conftest.py).
"""

import pytest

from api.services.progression import repository


@pytest.mark.asyncio
async def test_adding_exercise_stores_a_prescription(client, auth_headers, seeded_history):
    """У упражнения с историей предписание появляется сразу при добавлении."""
    workout = (
        await client.post("/workouts/start", headers=auth_headers, json={"source": "free"})
    ).json()
    resp = await client.post(
        f"/workouts/{workout['id']}/exercises",
        headers=auth_headers,
        json={"exercise_id": seeded_history.id},
    )
    assert resp.status_code == 200, resp.text

    added = resp.json()["exercises"][-1]
    assert added["prescription"] is not None
    assert added["prescription"]["reason_code"]
    assert added["recommended_weight"] is not None


@pytest.mark.asyncio
async def test_prescription_is_not_overwritten_on_repeat_calls(client, auth_headers, seeded_history):
    workout = (
        await client.post("/workouts/start", headers=auth_headers, json={"source": "free"})
    ).json()
    resp = await client.post(
        f"/workouts/{workout['id']}/exercises",
        headers=auth_headers,
        json={"exercise_id": seeded_history.id},
    )
    first = resp.json()["exercises"][-1]["prescription"]

    # Повторный расчёт через публичный эндпоинт не должен подменить сохранённое.
    se_id = resp.json()["exercises"][-1]["id"]
    await client.get(
        f"/workout-session-exercises/{se_id}/autoprogression", headers=auth_headers
    )

    again = (
        await client.get(f"/workouts/{workout['id']}", headers=auth_headers)
    ).json()["exercises"][-1]["prescription"]
    assert again == first


@pytest.mark.asyncio
async def test_finishing_a_session_refreshes_state_and_next_prescription(
    client, auth_headers, seeded_history, db, test_user
):
    workout = (
        await client.post("/workouts/start", headers=auth_headers, json={"source": "free"})
    ).json()
    add = (
        await client.post(
            f"/workouts/{workout['id']}/exercises",
            headers=auth_headers,
            json={"exercise_id": seeded_history.id},
        )
    ).json()
    se_id = add["exercises"][-1]["id"]

    await client.post(
        f"/workout-session-exercises/{se_id}/sets",
        headers=auth_headers,
        json={"weight": 40.0, "reps": 12, "effort_level": "medium"},
    )
    finish_resp = await client.post(
        f"/workouts/{workout['id']}/finish", headers=auth_headers
    )
    assert finish_resp.status_code == 200, finish_resp.text

    nxt = await repository.load_next_prescription(db, test_user.id, seeded_history.id)
    assert nxt is not None
    assert nxt.provisional is True
    assert nxt.reason_code


@pytest.mark.asyncio
async def test_exercise_without_history_gets_no_basis_not_a_crash(
    client, auth_headers, fresh_exercise
):
    workout = (
        await client.post("/workouts/start", headers=auth_headers, json={"source": "free"})
    ).json()
    resp = await client.post(
        f"/workouts/{workout['id']}/exercises",
        headers=auth_headers,
        json={"exercise_id": fresh_exercise.id},
    )
    assert resp.status_code == 200, resp.text
    added = resp.json()["exercises"][-1]
    assert added["recommended_weight"] is None
