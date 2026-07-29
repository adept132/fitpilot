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
from api.services.progression.metrics import e1rm
from api.services.models import UserExerciseProgressionState
from sqlalchemy import select


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

    # Вес НАМЕРЕННО сильно отличается от seeded_history (3x12x40кг, medium):
    # 60кг x8, medium даёт e1RM заметно выше, чем сессия из фикстуры. Это
    # ключ ко всей проверке ниже — если бы вес совпадал с seeded_history,
    # working_e1rm вышел бы одинаковым в обоих порядках выполнения кода и
    # ничего бы не различал.
    await client.post(
        f"/workout-session-exercises/{se_id}/sets",
        headers=auth_headers,
        json={"weight": 60.0, "reps": 8, "effort_level": "medium"},
    )
    finish_resp = await client.post(
        f"/workouts/{workout['id']}/finish", headers=auth_headers
    )
    assert finish_resp.status_code == 200, finish_resp.text

    nxt = await repository.load_next_prescription(db, test_user.id, seeded_history.id)
    assert nxt is not None
    assert nxt.provisional is True
    assert nxt.reason_code

    # --- Различающая проверка порядка (см. докстринг модуля и ревью) ---
    #
    # nxt.provisional/reason_code выше НЕ различают правильный и сломанный
    # порядок: seeded_history сама по себе даёт непустой базис, поэтому эти
    # поля непустые в обоих случаях (ревьюер проверил экспериментом — тест
    # проходил даже с переставленным циклом пересчёта).
    #
    # working_e1rm кэша (UserExerciseProgressionState) — различает надёжно.
    # rebuild_state (api/services/progression/state.py) идёт по истории в
    # хронологическом порядке и на каждой пригодной сессии ПЕРЕЗАПИСЫВАЕТ
    # working значением e1RM этой сессии — в переменной остаётся e1RM
    # ПОСЛЕДНЕЙ по времени сессии. load_history() отбирает только сессии со
    # status == "finished" (api/services/progression/repository.py).
    #
    # Если порядок в finish_workout правильный (status/finished_at выставлены
    # ДО пересчёта), только что залогированная сессия 60кг x8 уже "finished"
    # и попадает в историю как самая свежая — working_e1rm посчитается по
    # ней. Если порядок сломан (пересчёт раньше), эта сессия всё ещё "active"
    # и в историю не попадёт — working_e1rm останется от seeded_history
    # (40кг x12), то есть заметно меньше.
    row = (
        await db.execute(
            select(UserExerciseProgressionState).where(
                UserExerciseProgressionState.app_user_id == test_user.id,
                UserExerciseProgressionState.exercise_id == seeded_history.id,
            )
        )
    ).scalars().first()
    assert row is not None

    # RIR для effort_level="medium" — 2 (api/services/progression/params.py,
    # EFFORT_TO_RIR); формула e1RM — api/services/progression/metrics.py.
    expected_with_current_session = e1rm(60.0, 8, 2)
    expected_without_current_session = e1rm(40.0, 12, 2)
    # Подстраховка: если бы кто-то поменял веса фикстуры/сессии так, что
    # значения e1RM случайно совпали, сам тест перестал бы что-либо
    # различать — эта проверка не даст этому пройти незамеченным.
    assert expected_with_current_session != pytest.approx(
        expected_without_current_session
    )

    assert row.working_e1rm == pytest.approx(expected_with_current_session)
    assert row.working_e1rm != pytest.approx(expected_without_current_session)


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


@pytest.mark.asyncio
async def test_starting_workout_from_plan_stores_prescription(
    client, auth_headers, seeded_plan
):
    """P0-06 C1: POST /workouts/start с plan_id — единственный на момент
    финального ревью путь создания WorkoutSessionExercise, который НЕ вызывал
    движок вообще. Все существующие тесты этого модуля стартуют тренировку
    {"source": "free"} без plan_id и не могли поймать эту дыру — движок
    подключался только на add_exercise_to_workout (свободное добавление).

    seeded_plan содержит одно упражнение (seeded_history) с уже завершённой
    историей — есть база для непустого предписания, а не бутстрап no_basis
    без recommended_weight.
    """
    resp = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={"source": "free", "plan_id": seeded_plan.id},
    )
    assert resp.status_code == 200, resp.text
    workout_id = resp.json()["id"]

    detail = (
        await client.get(f"/workouts/{workout_id}", headers=auth_headers)
    ).json()
    exercises = detail["exercises"]
    assert exercises, "план должен был создать хотя бы одно упражнение в сессии"

    for ex in exercises:
        assert ex["prescription"] is not None, (
            "предписание не посчиталось для упражнения, созданного из плана — "
            "движок не подключён к плановому пути (P0-06 C1)"
        )
        assert ex["prescription"]["reason_code"]
        assert ex["recommended_weight"] is not None
