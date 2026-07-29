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


@pytest.mark.asyncio
async def test_plan_target_sets_reaches_prescription(
    client, auth_headers, seeded_plan_five_sets
):
    """Блокер 2 (финальное ревью P0-06): target_sets плана (=5) обязан
    попасть и в саму строку WorkoutSessionExercise, и в предписание — до
    фикса build_context брал дефолт (session_exercise.target_sets or 3), и
    предписание считалось на 3 подхода независимо от того, что говорил план.

    seeded_plan_five_sets — тот же план, что и seeded_plan, но с
    target_sets=5, чтобы не совпасть с дефолтом build_context случайно.
    """
    resp = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={"source": "free", "plan_id": seeded_plan_five_sets.id},
    )
    assert resp.status_code == 200, resp.text
    workout_id = resp.json()["id"]

    detail = (
        await client.get(f"/workouts/{workout_id}", headers=auth_headers)
    ).json()
    exercises = detail["exercises"]
    assert exercises, "план должен был создать хотя бы одно упражнение в сессии"

    ex = exercises[0]
    assert ex["target_sets"] == 5, ex
    assert ex["prescription"] is not None
    assert len(ex["prescription"]["sets"]) == 5, ex["prescription"]


@pytest.mark.asyncio
async def test_deload_phase_reaches_persisted_prescription(
    client, auth_headers, seeded_history, deload_phase_mesocycle
):
    """Блокер 3, тест 1 (финальное ревью P0-06): фаза мезоцикла обязана
    резолвиться из БД на ПИШУЩЕМ пути (добавление упражнения в активную
    сессию), а не только в юнит-тестах resolve_scheme
    (tests/test_progression_resolve.py), которые задают phase_effort_tier
    напрямую в SchemeContext и проходят даже без единой строчки резолва
    фазы из БД — именно так дефект C2 ("phase_effort_tier не резолвится ни
    на одном пишущем пути") проскочил через 19 ревью.

    Сессия стартует свободной ("source": "free") — workout_center.py::
    start_workout, ветка "ИНТЕЛЛЕКТУАЛЬНЫЙ ПОИСК КОНТЕКСТА", сама подхватывает
    активный мезоцикл пользователя (deload_phase_mesocycle.is_active=True) и
    проставляет app_user_mesocycle_id/mesocycle_phase на WorkoutSession.
    Упражнение добавляется уже в эту сессию, поэтому add_exercise_to_workout
    обязан прокинуть их в build_context через resolve_phase_effort_tier —
    иначе получит дефолт "medium" и правило deload_phase (reduction.py)
    останется мёртвым кодом, как до фикса C2.
    """
    workout = (
        await client.post("/workouts/start", headers=auth_headers, json={"source": "free"})
    ).json()
    resp = await client.post(
        f"/workouts/{workout['id']}/exercises",
        headers=auth_headers,
        json={"exercise_id": seeded_history.id},
    )
    assert resp.status_code == 200, resp.text

    prescription = resp.json()["exercises"][-1]["prescription"]
    assert prescription is not None
    assert prescription["reason_code"] == "deload_phase", prescription


@pytest.mark.asyncio
async def test_second_plan_workout_exits_bootstrap_scheme(
    client, auth_headers, seeded_plan, seeded_history
):
    """Блокер 3, тест 2 (финальное ревью P0-06): вторая тренировка из того же
    плана обязана выйти из бутстрапа e1rm_factor.

    После первой сессии из плана в истории упражнения появляется сохранённое
    предписание (persist_prescription вызывается сразу при создании
    WorkoutSessionExercise из плана, C1), и _has_prescription_history
    (api/services/progression/resolve.py) видит его — resolve_scheme больше
    не обязан скатываться в бутстрап.

    Существующий test_starting_workout_from_plan_stores_prescription
    проверяет только "предписание непустое" — это условие выполняется и
    бутстрапом (reason_code=bootstrap_no_prescription для ПЕРВОЙ сессии),
    поэтому он не ловит регрессию "движок навсегда застрял в бутстрапе".
    Этот тест смотрит на scheme/reason_code именно ВТОРОЙ сессии.
    """
    first = (
        await client.post(
            "/workouts/start",
            headers=auth_headers,
            json={"source": "free", "plan_id": seeded_plan.id},
        )
    ).json()
    first_detail = (
        await client.get(f"/workouts/{first['id']}", headers=auth_headers)
    ).json()
    first_ex = first_detail["exercises"][0]
    assert first_ex["exercise"]["id"] == seeded_history.id

    await client.post(
        f"/workout-session-exercises/{first_ex['id']}/sets",
        headers=auth_headers,
        json={"weight": 42.5, "reps": 12, "effort_level": "medium"},
    )
    finish = await client.post(f"/workouts/{first['id']}/finish", headers=auth_headers)
    assert finish.status_code == 200, finish.text

    second = (
        await client.post(
            "/workouts/start",
            headers=auth_headers,
            json={"source": "free", "plan_id": seeded_plan.id},
        )
    ).json()
    second_detail = (
        await client.get(f"/workouts/{second['id']}", headers=auth_headers)
    ).json()
    second_ex = second_detail["exercises"][0]
    assert second_ex["exercise"]["id"] == seeded_history.id

    prescription = second_ex["prescription"]
    assert prescription is not None
    assert prescription["scheme"] != "e1rm_factor", prescription
    assert prescription["reason_code"] != "bootstrap_no_prescription", prescription
