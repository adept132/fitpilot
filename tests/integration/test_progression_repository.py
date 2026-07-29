"""Репозиторий движка прогрессии: загрузка истории, write-once, проекция."""

import pytest

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
