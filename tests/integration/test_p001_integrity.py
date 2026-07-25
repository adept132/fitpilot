"""Интеграционные тесты валидатора аномалий и аналитических эндпоинтов (P0-01 / Task 7b).

Закрывает дыру в покрытии: путь пометки аномалий в БД и эндпоинты
readiness/discipline раньше не имели интеграционных тестов вообще. Идёт против
реальной Postgres (см. tests/integration/conftest.py).

Порог "абсурдного" веса — ABSURD_WEIGHT_KG=600 кг (api/services/anomaly_guard.py).
Схемы запросов (AddWorkoutSetRequest/SyncSetSnapshot) ограничивают вес сверху
le=2000, поэтому для теста используем вес 700 кг — он одновременно проходит
валидацию Pydantic и превышает абсолютный порог анти-аномалии.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.services.anomaly_stats import load_exercise_stats
from api.services.autoprogression import get_last_performance_basis_sets
from api.services.exercise_search_service import ExerciseSearchService
from api.services.models import (
    Exercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)

pytestmark = pytest.mark.asyncio

# Вес, который проходит валидацию Pydantic (le=2000), но физически невозможен
# по правилам anomaly_guard (порог 600 кг).
ABSURD_BUT_SCHEMA_VALID_WEIGHT = 700


async def _get_catalog_exercise(db) -> Exercise:
    exercise = (await db.execute(select(Exercise).limit(1))).scalar_one_or_none()
    assert exercise is not None, "в справочнике должно быть хотя бы одно упражнение"
    return exercise


async def _make_finished_session_with_sets(db, user_id, exercise_id, sets_spec, started_at=None):
    """Создаёт завершённую сессию с одним упражнением и перечисленными подходами.

    sets_spec — список словарей {weight, reps, set_type, is_anomalous, is_completed}.
    """
    session = WorkoutSession(
        app_user_id=user_id,
        source="free",
        status="finished",
        started_at=started_at or datetime.now(timezone.utc),
    )
    db.add(session)
    await db.flush()

    se = WorkoutSessionExercise(
        workout_session_id=session.id, exercise_id=exercise_id, order_index=0
    )
    db.add(se)
    await db.flush()

    for i, spec in enumerate(sets_spec, start=1):
        db.add(
            WorkoutSessionSet(
                workout_session_exercise_id=se.id,
                set_number=i,
                set_type=spec.get("set_type", "normal"),
                weight=spec.get("weight"),
                reps=spec.get("reps"),
                is_completed=spec.get("is_completed", True),
                is_anomalous=spec.get("is_anomalous", False),
            )
        )
    await db.flush()
    return session


async def _make_active_session(db, user_id):
    """Активная сессия без упражнений — для теста POST /workouts/{id}/exercises."""
    session = WorkoutSession(
        app_user_id=user_id,
        source="free",
        status="active",
        started_at=datetime.now(timezone.utc),
    )
    db.add(session)
    await db.flush()
    return session


async def _make_active_session_with_exercise(db, user_id, exercise_id):
    """Создаёт активную (незавершённую) сессию с одним упражнением без подходов —
    ровно то, что нужно эндпоинтам add-set/update-set."""
    session = WorkoutSession(
        app_user_id=user_id,
        source="free",
        status="active",
        started_at=datetime.now(timezone.utc),
    )
    db.add(session)
    await db.flush()

    se = WorkoutSessionExercise(
        workout_session_id=session.id, exercise_id=exercise_id, order_index=0
    )
    db.add(se)
    await db.flush()
    return session, se


# --- A. anomaly_stats.load_exercise_stats ------------------------------------


async def test_load_exercise_stats_computes_sessions_e1rm_median(db, test_user):
    """sessions/best_e1rm/median_weight_kg считаются по рабочим normal-подходам;
    warmup и уже помеченные аномалии исключаются — иначе одна ошибка
    расширила бы статистический коридор для следующих подходов."""
    exercise = await _get_catalog_exercise(db)

    # Сессия 1: рабочий подход (100кг/10) + аномальный (999кг), который обязан
    # быть исключён (иначе полностью исказил бы и e1rm, и медиану).
    await _make_finished_session_with_sets(
        db, test_user.id, exercise.id,
        [
            {"weight": 100, "reps": 10},
            {"weight": 999, "reps": 10, "is_anomalous": True},
        ],
    )
    # Сессия 2: рабочий подход (120кг/8, даёт лучший e1rm=152) + разминка
    # (200кг/20), которая тоже обязана быть исключена.
    await _make_finished_session_with_sets(
        db, test_user.id, exercise.id,
        [
            {"weight": 120, "reps": 8},
            {"weight": 200, "reps": 20, "set_type": "warmup"},
        ],
    )
    # Сессия 3: рабочий подход (110кг/5).
    await _make_finished_session_with_sets(
        db, test_user.id, exercise.id,
        [
            {"weight": 110, "reps": 5},
        ],
    )
    await db.commit()

    stats = await load_exercise_stats(db, test_user.id, exercise.id)

    assert stats.sessions == 3
    # best_e1rm = max(100*(1+10/30), 120*(1+8/30), 110*(1+5/30)) = 152.0 (сессия 2).
    # Если бы аномалия/разминка не исключались, максимум ушёл бы к 999 или 200 кг.
    assert stats.best_e1rm == pytest.approx(152.0)
    # Медиана весов [100, 110, 120] (без 999 и без разминочных 200) = 110.
    assert stats.median_weight_kg == pytest.approx(110.0)


async def test_load_exercise_stats_empty_history_returns_none(db, test_user):
    """У свежего пользователя без истории по упражнению — sessions=0, метрики None."""
    exercise = await _get_catalog_exercise(db)

    stats = await load_exercise_stats(db, test_user.id, exercise.id)

    assert stats.sessions == 0
    assert stats.best_e1rm is None
    assert stats.median_weight_kg is None


# --- B. REST anomaly flagging на добавлении подхода --------------------------


async def test_add_set_flags_absurd_weight_and_leaves_normal_clean(client, db, test_user):
    """POST добавления подхода: абсурдный вес сохраняется (не отклоняется), но
    помечается is_anomalous=True; обычный вес остаётся is_anomalous=False."""
    exercise = await _get_catalog_exercise(db)
    _, se = await _make_active_session_with_exercise(db, test_user.id, exercise.id)
    se_id = se.id
    await db.commit()

    absurd = await client.post(
        f"/workout-session-exercises/{se_id}/sets",
        json={"set_type": "normal", "weight": ABSURD_BUT_SCHEMA_VALID_WEIGHT, "reps": 5},
    )
    # Ключевой момент: подход СОХРАНЯЕТСЯ (не 4xx/5xx), а не отклоняется.
    assert absurd.status_code == 200, absurd.text
    assert absurd.json()["is_anomalous"] is True

    normal = await client.post(
        f"/workout-session-exercises/{se_id}/sets",
        json={"set_type": "normal", "weight": 80, "reps": 8},
    )
    assert normal.status_code == 200, normal.text
    assert normal.json()["is_anomalous"] is False


# --- C. REST anomaly flagging на обновлении подхода ---------------------------


async def test_update_set_flags_anomaly_based_on_post_update_state(client, db, test_user):
    """PATCH: подход создаётся нормальным (is_anomalous=False), затем правится
    на абсурдное значение — вердикт обязан отражать НОВОЕ состояние, а не то,
    с которым подход был создан."""
    exercise = await _get_catalog_exercise(db)
    _, se = await _make_active_session_with_exercise(db, test_user.id, exercise.id)
    se_id = se.id
    await db.commit()

    created = await client.post(
        f"/workout-session-exercises/{se_id}/sets",
        json={"set_type": "normal", "weight": 60, "reps": 8},
    )
    assert created.status_code == 200, created.text
    assert created.json()["is_anomalous"] is False
    set_id = created.json()["id"]

    updated = await client.patch(
        f"/workout-session-sets/{set_id}",
        json={"weight": ABSURD_BUT_SCHEMA_VALID_WEIGHT},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["is_anomalous"] is True
    assert float(updated.json()["weight"]) == pytest.approx(float(ABSURD_BUT_SCHEMA_VALID_WEIGHT))


# --- D. /sync/workouts никогда не 422 на аномалии (ключевой инвариант) --------


async def test_sync_workouts_never_422s_on_anomaly_but_flags_it(client, db, test_user):
    """Снимок с абсурдным весом обязан пройти синк с 200 (не 409/422) — один
    кривой подход не должен разваливать весь пуш тренировки; при этом подход
    персистится с is_anomalous=True."""
    exercise = await _get_catalog_exercise(db)

    client_uuid = uuid.uuid4().hex
    set_uuid = f"set-{client_uuid}"
    snapshot = {
        "client_uuid": client_uuid,
        "base_version": None,
        "source": "free",
        "status": "active",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "deleted": False,
        "exercises": [
            {
                "client_uuid": f"ex-{client_uuid}",
                "exercise_id": exercise.id,
                "order_index": 0,
                "sets": [
                    {
                        "client_uuid": set_uuid,
                        "set_number": 1,
                        "weight": ABSURD_BUT_SCHEMA_VALID_WEIGHT,
                        "reps": 5,
                    }
                ],
            }
        ],
    }

    resp = await client.post("/sync/workouts", json=snapshot)
    # Инвариант P0-03: синк только помечает аномалию, не поднимает HTTPException.
    assert resp.status_code == 200, resp.text

    workout_set = (
        await db.execute(
            select(WorkoutSessionSet).where(WorkoutSessionSet.client_uuid == set_uuid)
        )
    ).scalar_one_or_none()
    assert workout_set is not None
    assert workout_set.is_anomalous is True


# --- E. /progress/discipline density guards -----------------------------------


async def _make_session_for_discipline(db, user_id, exercise_id, *, started_at, finished_at, sets_count):
    session = WorkoutSession(
        app_user_id=user_id,
        source="free",
        status="finished" if finished_at is not None else "active",
        started_at=started_at,
        finished_at=finished_at,
    )
    db.add(session)
    await db.flush()

    se = WorkoutSessionExercise(
        workout_session_id=session.id, exercise_id=exercise_id, order_index=0
    )
    db.add(se)
    await db.flush()

    for i in range(1, sets_count + 1):
        db.add(
            WorkoutSessionSet(
                workout_session_exercise_id=se.id,
                set_number=i,
                set_type="normal",
                weight=50,
                reps=10,
                is_completed=True,
            )
        )
    await db.flush()
    return session


async def test_discipline_density_guards(client, db, test_user):
    """Гварды плотности: правдоподобная длительность считается один раз на
    сессию (не на подход); сессии без finished_at и с абсурдной длительностью
    (20 часов) в плотность не попадают; календарь покрывает весь запрошенный
    период (включая пустые дни)."""
    exercise = await _get_catalog_exercise(db)
    now = datetime.now(timezone.utc)

    # Три "правдоподобные" сессии (45, 50, 40 минут) — ровно MIN_SESSIONS_FOR_DENSITY.
    await _make_session_for_discipline(
        db, test_user.id, exercise.id,
        started_at=now - timedelta(hours=5),
        finished_at=now - timedelta(hours=5) + timedelta(minutes=45),
        sets_count=2,
    )
    await _make_session_for_discipline(
        db, test_user.id, exercise.id,
        started_at=now - timedelta(hours=10),
        finished_at=now - timedelta(hours=10) + timedelta(minutes=50),
        sets_count=3,
    )
    await _make_session_for_discipline(
        db, test_user.id, exercise.id,
        started_at=now - timedelta(hours=15),
        finished_at=now - timedelta(hours=15) + timedelta(minutes=40),
        sets_count=1,
    )
    # Незавершённая сессия — не должна попасть в плотность вовсе.
    await _make_session_for_discipline(
        db, test_user.id, exercise.id,
        started_at=now - timedelta(hours=20),
        finished_at=None,
        sets_count=5,
    )
    # Абсурдная длительность (20 часов) — тоже должна быть исключена.
    absurd_start = now - timedelta(hours=30)
    await _make_session_for_discipline(
        db, test_user.id, exercise.id,
        started_at=absurd_start,
        finished_at=absurd_start + timedelta(hours=20),
        sets_count=4,
    )
    await db.commit()

    resp = await client.get("/progress/discipline", params={"weeks": 13})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Календарь покрывает запрошенное окно (13 * 7 дней), включая пустые, плюс
    # добор назад до понедельника: клиентская heatmap рисует колонками по неделям,
    # и окно, начатое с середины недели, оставляло бы дыры в первой колонке.
    days = body["days"]
    assert 13 * 7 <= len(days) <= 13 * 7 + 6
    # Первый день окна — всегда понедельник (weekday() == 0).
    first_day = datetime.strptime(days[0]["date"], "%Y-%m-%d")
    assert first_day.weekday() == 0, f"окно начинается не с понедельника: {days[0]['date']}"
    # Дни идут подряд, без пропусков.
    for i in range(1, len(days)):
        prev = datetime.strptime(days[i - 1]["date"], "%Y-%m-%d")
        cur = datetime.strptime(days[i]["date"], "%Y-%m-%d")
        assert (cur - prev).days == 1, f"разрыв в календаре: {days[i-1]['date']} -> {days[i]['date']}"
    assert any(day["sets"] == 0 for day in days)

    density = body["density"]
    # Только 3 правдоподобные сессии участвуют — не 5.
    assert density["sessions_28d"] == 3
    # 2+3+1=6 подходов за 45+50+40=135 минут (2.25ч) => 6/2.25 = 2.666... -> 2.7.
    # Если бы длительность считалась на КАЖДЫЙ подход (а не один раз на сессию),
    # знаменатель раздулся бы (2*45+3*50+1*40=280мин) и число не совпало бы.
    assert density["sets_per_hour_28d"] == pytest.approx(2.7)
    # Медиана длительностей [40, 45, 50] = 45.
    assert density["median_duration_min"] == pytest.approx(45.0)


async def test_discipline_density_null_below_min_sessions(client, db, test_user):
    """Меньше MIN_SESSIONS_FOR_DENSITY (3) правдоподобных сессий -> плотность
    отдаётся null, а не считается по недостаточной выборке."""
    exercise = await _get_catalog_exercise(db)
    now = datetime.now(timezone.utc)

    await _make_session_for_discipline(
        db, test_user.id, exercise.id,
        started_at=now - timedelta(hours=5),
        finished_at=now - timedelta(hours=5) + timedelta(minutes=45),
        sets_count=2,
    )
    await _make_session_for_discipline(
        db, test_user.id, exercise.id,
        started_at=now - timedelta(hours=10),
        finished_at=now - timedelta(hours=10) + timedelta(minutes=50),
        sets_count=3,
    )
    await db.commit()

    resp = await client.get("/progress/discipline", params={"weeks": 13})
    assert resp.status_code == 200, resp.text
    density = resp.json()["density"]

    assert density["sessions_28d"] == 2
    assert density["sets_per_hour_28d"] is None
    assert density["median_duration_min"] is None


# --- F. /progress/readiness cold-start guard ----------------------------------


async def test_readiness_cold_start_with_short_history(client, db, test_user):
    """С парой дней реальной истории (< cold_start_days=14) confidence обязан
    быть cold_start, а systemic.z — None: без истории z — это шум."""
    exercise = await _get_catalog_exercise(db)
    now = datetime.now(timezone.utc)

    for started_at in (now - timedelta(days=1), now):
        session = WorkoutSession(
            app_user_id=test_user.id,
            source="free",
            status="finished",
            started_at=started_at,
            finished_at=started_at + timedelta(minutes=45),
        )
        db.add(session)
        await db.flush()

        se = WorkoutSessionExercise(
            workout_session_id=session.id, exercise_id=exercise.id, order_index=0
        )
        db.add(se)
        await db.flush()

        db.add(
            WorkoutSessionSet(
                workout_session_exercise_id=se.id,
                set_number=1,
                set_type="normal",
                weight=60,
                reps=8,
                is_completed=True,
            )
        )
        await db.flush()

    await db.commit()

    resp = await client.get("/progress/readiness")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["confidence"] == "cold_start"
    assert body["systemic"]["z"] is None


# --- G. Аномальные подходы исключены из автопрогрессии и аналитики (спека §5.3) --


async def test_autoprogression_basis_sets_excludes_anomalous(db, test_user):
    """get_last_performance_basis_sets не должна отдавать аномальный подход как
    базу для автопрогрессии: один жим «700 кг» иначе задрал бы e1RM и
    рекомендованный вес на следующей тренировке."""
    exercise = await _get_catalog_exercise(db)

    await _make_finished_session_with_sets(
        db, test_user.id, exercise.id,
        [
            {"weight": 100, "reps": 10},
            {"weight": ABSURD_BUT_SCHEMA_VALID_WEIGHT, "reps": 5, "is_anomalous": True},
        ],
    )
    await db.commit()

    basis = await get_last_performance_basis_sets(db, test_user.id, exercise.id)

    assert len(basis) == 1
    assert float(basis[0].weight) == pytest.approx(100.0)
    assert all(not s.is_anomalous for s in basis)


async def test_analytics_history_excludes_anomalous(db, test_user):
    """get_exercise_analytics_history не должна включать аномальные подходы в
    график e1RM/объёма (спека §5.3): без исключения объём и e1rm за день были бы
    искажены абсурдным весом."""
    exercise = await _get_catalog_exercise(db)

    session = await _make_finished_session_with_sets(
        db, test_user.id, exercise.id,
        [
            {"weight": 100, "reps": 10},
            {"weight": ABSURD_BUT_SCHEMA_VALID_WEIGHT, "reps": 5, "is_anomalous": True},
        ],
    )
    session.finished_at = datetime.now(timezone.utc)
    await db.commit()

    history = await ExerciseSearchService.get_exercise_analytics_history(
        db, test_user.id, exercise.id
    )

    assert history is not None
    assert len(history["history"]) == 1
    day = history["history"][0]
    # Только нормальный подход (100кг х 10) должен попасть в дневную сводку.
    assert day["sets"] == [{"weight": 100, "reps": 10}]
    # e1rm = 100 * (1 + 10/30) = 133.3; если бы аномалия не исключалась, e1rm
    # ушёл бы к абсурдному весу (700 кг).
    assert day["e1rm"] == pytest.approx(133.3)
    assert day["volume"] == pytest.approx(1000.0)


# --- H. recommended_weight заполняется при добавлении упражнения в сессию (F1) --


async def test_add_exercise_populates_recommended_weight_with_basis(client, db, test_user):
    """POST /workouts/{id}/exercises обязан рассчитать и сохранить
    recommended_weight, если у пользователя уже есть завершённая тренировка с
    этим упражнением (база для автопрогрессии)."""
    exercise = await _get_catalog_exercise(db)

    # Завершённая сессия с рабочими подходами — база для автопрогрессии.
    await _make_finished_session_with_sets(
        db, test_user.id, exercise.id,
        [
            {"weight": 100, "reps": 10},
            {"weight": 105, "reps": 8},
        ],
    )

    workout = await _make_active_session(db, test_user.id)
    workout_id = workout.id
    await db.commit()

    resp = await client.post(
        f"/workouts/{workout_id}/exercises",
        json={"exercise_id": exercise.id},
    )
    assert resp.status_code == 200, resp.text

    session_exercise = (
        await db.execute(
            select(WorkoutSessionExercise).where(
                WorkoutSessionExercise.workout_session_id == workout_id,
                WorkoutSessionExercise.exercise_id == exercise.id,
            )
        )
    ).scalar_one_or_none()

    assert session_exercise is not None
    assert session_exercise.recommended_weight is not None
    # Правдоподобный вес: не ноль/отрицательный и в разумных пределах от базы (100-105 кг).
    assert 0 < float(session_exercise.recommended_weight) < 500


async def test_add_exercise_recommended_weight_null_without_basis(client, db, test_user):
    """Без предыдущей завершённой тренировки с этим упражнением базы для
    автопрогрессии нет — recommended_weight обязан остаться null (не 0 и не
    выдуманное значение)."""
    exercise = await _get_catalog_exercise(db)

    workout = await _make_active_session(db, test_user.id)
    workout_id = workout.id
    await db.commit()

    resp = await client.post(
        f"/workouts/{workout_id}/exercises",
        json={"exercise_id": exercise.id},
    )
    assert resp.status_code == 200, resp.text

    session_exercise = (
        await db.execute(
            select(WorkoutSessionExercise).where(
                WorkoutSessionExercise.workout_session_id == workout_id,
                WorkoutSessionExercise.exercise_id == exercise.id,
            )
        )
    ).scalar_one_or_none()

    assert session_exercise is not None
    assert session_exercise.recommended_weight is None
