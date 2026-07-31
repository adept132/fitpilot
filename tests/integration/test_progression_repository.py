"""Репозиторий движка прогрессии: загрузка истории, write-once, проекция."""

from datetime import datetime, timezone

import pytest

from api.services.models import Exercise, WorkoutSession, WorkoutSessionExercise
from api.services.progression import repository
from api.services.progression.types import Prescription, SetPrescription


def sample_prescription(weight=42.5) -> Prescription:
    return Prescription(
        scheme="double",
        sets=(
            SetPrescription(1, weight, 8, 12, 2, "normal"),
            SetPrescription(2, weight, 8, 12, 2, "normal"),
        ),
        reason_code="progressed",
        reason_text="Прошлая цель выполнена — двигаемся вперёд.",
        basis={"anchor_weight": 40.0},
    )


@pytest.mark.asyncio
async def test_persist_writes_prescription_and_projection(db_session, session_exercise):
    repository.persist_prescription(session_exercise, sample_prescription())
    await db_session.flush()

    assert session_exercise.prescription["scheme"] == "double"
    # Проекция первого рабочего подхода — для fatigue/, csv_format.py и
    # старых клиентов.
    assert float(session_exercise.recommended_weight) == pytest.approx(42.5)
    assert session_exercise.recommended_rep_min == 8
    assert session_exercise.recommended_rep_max == 12
    assert session_exercise.recommended_rir == 2


@pytest.mark.asyncio
async def test_projection_carries_the_trimmed_set_count(db_session, session_exercise):
    """P0-07: подрезка объёма должна быть ВИДНА пользователю.

    apply_volume_trim убирает подходы из Prescription.sets, но интерфейс
    показывает плоское target_sets. Без этой проекции причина «сократили
    число подходов» появлялась, а сама цифра не менялась.
    """
    session_exercise.target_sets = 4
    await db_session.flush()

    # Предписание на два подхода — как после трима с уровнем limit.
    repository.persist_prescription(session_exercise, sample_prescription())
    await db_session.flush()

    assert len(session_exercise.prescription["sets"]) == 2
    assert session_exercise.target_sets == 2


@pytest.mark.asyncio
async def test_persist_is_write_once(db_session, session_exercise):
    repository.persist_prescription(session_exercise, sample_prescription(42.5))
    await db_session.flush()
    repository.persist_prescription(session_exercise, sample_prescription(99.0))
    await db_session.flush()

    # Пользователь уже видел первое предписание — подменять его нельзя.
    assert float(session_exercise.recommended_weight) == pytest.approx(42.5)
    assert session_exercise.prescription["sets"][0]["weight_kg"] == pytest.approx(42.5)


@pytest.mark.asyncio
async def test_empty_prescription_does_not_overwrite_projection(
    db_session, session_exercise
):
    empty = Prescription(
        scheme="double", sets=(), reason_code="no_basis", reason_text="x"
    )
    repository.persist_prescription(session_exercise, empty)
    await db_session.flush()
    assert session_exercise.recommended_weight is None


@pytest.mark.asyncio
async def test_refresh_state_is_idempotent(db_session, app_user, exercise):
    await repository.refresh_state(db_session, app_user.id, exercise.id, None)
    await db_session.flush()
    await repository.refresh_state(db_session, app_user.id, exercise.id, None)
    await db_session.flush()

    rows = await repository.debug_all_states(db_session, app_user.id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_load_history_returns_newest_first(db_session, app_user, exercise):
    history = await repository.load_history(db_session, app_user.id, exercise.id)
    ids = [s.session_id for s in history.sessions]
    assert ids == sorted(ids, reverse=True)


# --- Блокер 1 финального ревью P0-06: принадлежность предписания упражнению ---
#
# full_replace (api/routers/exercises.py) меняет WorkoutSessionExercise.exercise_id
# на другое упражнение, но write-once prescription остаётся посчитанным для
# СТАРОГО. Без защиты упражнение без единой собственной тренировки получает
# чужую цель: evaluate() сравнивает факт с чужим предписанием, а
# _has_prescription_history() решает, что бутстрап уже пройден.


@pytest.mark.asyncio
async def test_persist_prescription_stamps_owner_exercise_id(db_session, session_exercise):
    """persist_prescription штампует exercise_id строки в basis — единственная
    подпись принадлежности, которая переживёт full_replace."""
    repository.persist_prescription(session_exercise, sample_prescription())
    await db_session.flush()

    assert (
        session_exercise.prescription["basis"]["exercise_id"]
        == session_exercise.exercise_id
    )


@pytest.mark.asyncio
async def test_load_history_rejects_prescription_from_other_exercise(
    db_session, app_user, exercise
):
    """Смоделированный full_replace: строка с prescription, посчитанным для
    other_ex, "переезжает" на exercise (exercise_id меняется, prescription —
    нет, как это реально делает write-once). load_history для exercise
    обязана деградировать эту сессию до "нет предписания", как для легитимно
    пустого se.prescription is None — тот же путь, что и для неразбираемого
    JSON (см. комментарий в load_history)."""
    other_ex = Exercise(
        name="Другое упражнение (P0-06, блокер 1)",
        category="base",
        main_muscle_group="back",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=app_user.id,
    )
    db_session.add(other_ex)
    await db_session.flush()

    workout = WorkoutSession(
        app_user_id=app_user.id,
        source="free",
        status="finished",
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(workout)
    await db_session.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id,
        exercise_id=other_ex.id,
        order_index=0,
    )
    db_session.add(se)
    await db_session.flush()

    # Предписание считается и штампуется, пока строка ещё принадлежит other_ex.
    repository.persist_prescription(se, sample_prescription())
    await db_session.flush()

    # full_replace: exercise_id строки меняется на exercise, prescription
    # (write-once) остаётся от other_ex.
    se.exercise_id = exercise.id
    await db_session.flush()

    history = await repository.load_history(db_session, app_user.id, exercise.id)
    assert len(history.sessions) == 1
    assert history.sessions[0].prescription is None, (
        "load_history доверилась предписанию другого упражнения — "
        "защита принадлежности (P0-06, блокер 1) не сработала"
    )


@pytest.mark.asyncio
async def test_load_history_trusts_prescription_without_owner_stamp(
    db_session, app_user, exercise
):
    """Обратная совместимость: у ВСЕХ предписаний, сохранённых до этого
    фикса, метки basis["exercise_id"] нет вовсе. Отсутствие метки — это НЕ
    признак чужого предписания, а "принадлежность неизвестна" -> доверяем,
    иначе одним махом обнулится вся накопленная история предписаний."""
    workout = WorkoutSession(
        app_user_id=app_user.id,
        source="free",
        status="finished",
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(workout)
    await db_session.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id,
        exercise_id=exercise.id,
        order_index=0,
    )
    db_session.add(se)
    await db_session.flush()

    # Легаси-запись: prescription сохранён МИМО persist_prescription (как
    # было бы у строк, осевших в БД до фикса) — в basis нет exercise_id.
    se.prescription = sample_prescription().to_dict()
    await db_session.flush()

    history = await repository.load_history(db_session, app_user.id, exercise.id)
    assert len(history.sessions) == 1
    assert history.sessions[0].prescription is not None
    assert history.sessions[0].prescription.scheme == "double"
